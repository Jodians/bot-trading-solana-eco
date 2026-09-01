"""
test_dashboard_fixes.py - Verifies the dashboard/telemetry/discovery fixes:

  1. ws_listener discovery: the pump.fun CreateEvent is decoded straight from the
     `Program data:` log line -> zero getTransaction calls; trades and failed txs
     are never mistaken for launches; each mint/signature is handled once.
  1b. an undecodable pump.fun event still falls back to getTransaction.
  1c. a mint whose metadata is not indexed yet is RETRIED, not dropped forever.
  1d. a slow metadata fetch cannot stall the WebSocket read loop.
  2-4. KPI invariant: passed + skipped == scanned (exactly one token_eval verdict
     per token_new), including the max-positions and LLM-reject paths.
  5. Re-delivered tokens already held as positions emit no telemetry at all.
  6. Paper entries are priced from a REAL Jupiter quote and are refused (no
     position, no fake P&L) when the token has no route yet.
  7. Position monitoring marks to market with real sell quotes, tolerates a
     transient quote failure, and abandons a position that stays unpriceable.

Pure unit tests - no network, no RPC, no real trades. Run:
    .venv/Scripts/python.exe test_dashboard_fixes.py
"""
import asyncio
import os
import sys

import config

# --- deterministic config for the tests -----------------------------------
cfg = config.cfg
cfg.LIVE_TRADING = False
cfg.MAX_OPEN_POSITIONS = 2
cfg.BUY_AMOUNT_SOL = 0.1
cfg.LLM_ANALYSIS_ENABLED = False

import snipe
import ws_listener
from telemetry import tel

# reset_bot() stubs snipe.monitor_position with a no-op so the token-intake tests
# never fire real Jupiter quotes. Capture the genuine coroutine here, before any
# stubbing, so the monitor tests can exercise the real mark-to-market loop.
_REAL_MONITOR = snipe.monitor_position

FAILURES = []


def check(label, cond, detail=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        FAILURES.append(label)


def meta(mint, name=None):
    return {"mint": mint, "name": name or mint[:6], "symbol": "TST",
            "usd_market_cap": 5000}


def reset_bot(evaluate_result=(True, "ok")):
    """Reset bot + telemetry state and stub out every network call."""
    snipe.positions.clear()
    snipe._monitors.clear()
    tel.feed.clear()
    tel.positions.clear()
    tel.pnl_history.clear()
    for k in ("scanned", "passed", "skipped", "buys", "exits_tp", "exits_sl"):
        tel.stats[k] = 0
    tel.stats["realized_pnl_sol"] = 0.0

    async def _eval(_m):
        return evaluate_result
    snipe.evaluate_token = _eval

    async def _buy(_mint, sol):
        return {"paper": True, "token_amount": 1000}
    snipe.buy_token = _buy

    # never let a monitor task fire real Jupiter quotes
    async def _monitor(_mint):
        return
    snipe.monitor_position = _monitor
    snipe.tg_enabled = lambda: False


# ---------------------------------------------------------------- test 1
class FakeWS:
    """Minimal async-iterable stand-in for a websockets connection."""

    def __init__(self, messages):
        self._messages = messages
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, data):
        self.sent.append(data)

    def __aiter__(self):
        async def gen():
            for m in self._messages:
                yield m
            raise _StopFeed()
        return gen()


class _StopFeed(BaseException):
    """Ends the fake feed. Inherits BaseException so ws_listen's `except
    Exception` reconnect handler does not swallow it into an endless retry."""


def notification(sig, logs, err=None):
    import json
    return json.dumps({
        "method": "logsNotification",
        "params": {"result": {"value": {"signature": sig, "logs": logs, "err": err}}},
    })


def create_logs(mint_raw, name="Tok", symbol="TK"):
    """Build a realistic pump.fun CreateEvent log frame for `mint_raw` (32 bytes)."""
    import base64, struct
    import pumpfun_events as E

    def bstr(s):
        b = s.encode()
        return struct.pack("<I", len(b)) + b

    payload = (E.CREATE_EVENT + bstr(name) + bstr(symbol) + bstr("ipfs://x")
               + mint_raw + os.urandom(32) + os.urandom(32) + b"\x00" * 40)
    return [
        f"Program {ws_listener.PUMP_PROGRAM} invoke [1]",
        "Program log: Instruction: Create",
        "Program data: " + base64.b64encode(payload).decode(),
        f"Program {ws_listener.PUMP_PROGRAM} success",
    ]


def trade_logs():
    """A pump.fun TRADE: carries a TradeEvent blob and a decoy CreateTokenAccount."""
    import base64
    import pumpfun_events as E
    payload = E.TRADE_EVENT + os.urandom(96)
    return [
        "Program log: Instruction: CreateTokenAccount",   # decoy for text filters
        f"Program {ws_listener.PUMP_PROGRAM} invoke [2]",
        "Program log: Instruction: Sell",
        "Program data: " + base64.b64encode(payload).decode(),
    ]


async def test_ws_dedup():
    print("test 1: ws_listener CreateEvent decode + dedup (zero RPC)")
    import pumpfun_events as E

    raw_a, raw_b = os.urandom(32), os.urandom(32)
    MINT_A, MINT_B = E.b58encode(raw_a), E.b58encode(raw_b)

    msgs = [
        notification("sig1", create_logs(raw_a, "Alpha", "ALP")),  # new -> forwarded
        notification("sig1", create_logs(raw_a)),                  # dup sig -> dropped
        notification("sig2", create_logs(raw_a)),                  # dup mint -> dropped
        notification("sig3", trade_logs()),                        # trade -> dropped
        notification("sig4", create_logs(raw_b, "Beta", "BET")),   # new -> forwarded
        notification("sig5", create_logs(os.urandom(32)), err={"InstructionError": 1}),
    ]

    rpc_calls = []

    async def fake_get_tx(sig):
        rpc_calls.append(sig)
        return {}

    async def fake_meta(mint):
        return {"mint": mint, "usd_market_cap": 5000}  # no name/symbol, like DexScreener

    ws_listener._get_transaction = fake_get_tx
    ws_listener._fetch_token_meta = fake_meta

    forwarded = []

    async def on_token(m):
        forwarded.append(m)

    fake = FakeWS(msgs)
    fake_mod = type(sys)("websockets")
    fake_mod.connect = lambda url: fake
    sys.modules["websockets"] = fake_mod
    try:
        await asyncio.wait_for(ws_listener.ws_listen(on_token, reconnect_delay=0.01), 5)
    except (asyncio.TimeoutError, _StopFeed):
        pass
    finally:
        sys.modules.pop("websockets", None)

    mints = [m["mint"] for m in forwarded]
    check("each mint forwarded exactly once", mints == [MINT_A, MINT_B], f"got {mints}")
    check("ZERO getTransaction calls (decode only)", rpc_calls == [], f"rpc={rpc_calls}")
    check("trade tx not treated as a launch", MINT_A in mints and len(mints) == 2)
    check("failed tx skipped", len(mints) == 2, f"got {mints}")
    check("event name/symbol fill gaps in meta",
          forwarded and forwarded[0].get("symbol") == "ALP", forwarded[:1])


async def test_ws_fallback_rpc():
    print("test 1b: undecodable pump.fun event falls back to getTransaction")
    import pumpfun_events as E
    import base64
    raw = os.urandom(32)
    MINT = E.b58encode(raw)

    # a pump.fun event blob with an unknown discriminator -> cannot decode,
    # but iter_program_data() sees it, so we must pay for one RPC call.
    weird = ["Program data: " + base64.b64encode(os.urandom(8) + os.urandom(60)).decode()]

    rpc_calls = []

    async def fake_get_tx(sig):
        rpc_calls.append(sig)
        return {"transaction": {"message": {"accountKeys": [MINT]}}, "meta": {}}

    async def fake_meta(mint):
        return {"mint": mint}

    ws_listener._get_transaction = fake_get_tx
    ws_listener._fetch_token_meta = fake_meta

    got = []

    async def on_token(m):
        got.append(m)

    fake = FakeWS([notification("sigX", weird)])
    fake_mod = type(sys)("websockets")
    fake_mod.connect = lambda url: fake
    sys.modules["websockets"] = fake_mod
    try:
        await asyncio.wait_for(ws_listener.ws_listen(on_token, reconnect_delay=0.01), 5)
    except (asyncio.TimeoutError, _StopFeed):
        pass
    finally:
        sys.modules.pop("websockets", None)

    check("fallback made exactly one RPC call", rpc_calls == ["sigX"], rpc_calls)
    check("fallback still yields the mint",
          [m["mint"] for m in got] == [MINT], got)


async def test_known_event_beside_unknown_blob_is_not_a_launch():
    """The frame that flooded the live run: a TradeEvent riding alongside an
    unrecognised blob from a router / CPI caller. Any recognised event proves the
    tx is not a launch, so it must cost ZERO RPC calls. Treating these as
    'maybe a launch' queued a fallback job per trade and swamped the queue.
    """
    print("test 1e: recognised event + unknown blob costs no RPC")
    import base64
    import pumpfun_events as E

    mixed = [
        f"Program {ws_listener.PUMP_PROGRAM} invoke [1]",
        "Program data: " + base64.b64encode(E.TRADE_EVENT + os.urandom(96)).decode(),
        "Program data: " + base64.b64encode(os.urandom(8) + os.urandom(40)).decode(),
    ]
    check("mixed frame is not flagged as a possible launch",
          not E.has_unknown_event(mixed))
    check("wholly unrecognised frame still is",
          E.has_unknown_event(
              ["Program data: " + base64.b64encode(os.urandom(48)).decode()]))

    rpc_calls = []

    async def fake_get_tx(sig):
        rpc_calls.append(sig)
        return {}

    ws_listener._get_transaction = fake_get_tx
    ws_listener._fetch_token_meta = lambda m: _amint(m)

    got = []

    async def on_token(m):
        got.append(m)

    fake = FakeWS([notification("sigMix", mixed)])
    fake_mod = type(sys)("websockets")
    fake_mod.connect = lambda url: fake
    sys.modules["websockets"] = fake_mod
    try:
        await asyncio.wait_for(ws_listener.ws_listen(on_token, reconnect_delay=0.01), 5)
    except (asyncio.TimeoutError, _StopFeed):
        pass
    finally:
        sys.modules.pop("websockets", None)

    check("no RPC fallback for a trade frame", rpc_calls == [], rpc_calls)
    check("nothing forwarded", got == [], got)


async def _amint(mint):
    return {"mint": mint}


# ---------------------------------------------------------------- test 1c
async def test_enrich_retries_unindexed_mint():
    """A brand-new mint is usually not indexed yet: pump.fun's API is Cloudflare
    blocked and DexScreener has no pair for a few seconds. The old code marked
    the mint seen BEFORE fetching metadata, so that first miss discarded the
    launch permanently - only tokens old enough to be indexed could be bought,
    the exact opposite of sniping. Enrichment must retry and still deliver."""
    print("test 1c: not-yet-indexed mint is retried, not dropped forever")
    import pumpfun_events as E

    raw = os.urandom(32)
    MINT = E.b58encode(raw)

    attempts = {"n": 0}

    async def flaky_meta(mint):
        # Fails the first two times (not indexed yet), then succeeds.
        attempts["n"] += 1
        if attempts["n"] < 3:
            return None
        return {"mint": mint, "usd_market_cap": 9000}

    async def fake_get_tx(sig):
        raise AssertionError("must not call getTransaction for a decoded create")

    ws_listener._fetch_token_meta = flaky_meta
    ws_listener._get_transaction = fake_get_tx
    orig_delay = ws_listener.ENRICH_RETRY_DELAY
    ws_listener.ENRICH_RETRY_DELAY = 0.01  # keep the test fast

    forwarded = []

    async def on_token(m):
        forwarded.append(m)

    fake = FakeWS([notification("sigR", create_logs(raw, "Late", "LATE"))])
    fake_mod = type(sys)("websockets")
    fake_mod.connect = lambda url: fake
    sys.modules["websockets"] = fake_mod
    try:
        await asyncio.wait_for(ws_listener.ws_listen(on_token, reconnect_delay=0.01), 5)
    except (asyncio.TimeoutError, _StopFeed):
        pass
    finally:
        sys.modules.pop("websockets", None)
        ws_listener.ENRICH_RETRY_DELAY = orig_delay

    check("retried until metadata appeared", attempts["n"] == 3, f"attempts={attempts['n']}")
    check("mint delivered exactly once despite retries",
          [m["mint"] for m in forwarded] == [MINT], forwarded)
    check("identity taken from the on-chain event",
          forwarded and forwarded[0].get("symbol") == "LATE", forwarded[:1])


async def test_reader_not_blocked_by_slow_enrichment():
    """Enrichment must not run on the socket read path. Awaiting HTTP inline is
    what produced 538 `keepalive ping timeout` disconnects in the run log: while
    the fetch was pending the loop stopped reading frames and missed the
    server's ping. The reader should consume the whole feed while a slow fetch
    is still in flight."""
    print("test 1d: slow metadata fetch does not stall the reader")
    import pumpfun_events as E

    raws = [os.urandom(32) for _ in range(5)]
    MINTS = [E.b58encode(r) for r in raws]
    read_done = asyncio.Event()
    release = asyncio.Event()

    async def slow_meta(mint):
        # Block until the reader has drained the feed, proving it kept reading.
        try:
            await asyncio.wait_for(read_done.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
        release.set()
        return {"mint": mint}

    ws_listener._fetch_token_meta = slow_meta

    class SignallingWS(FakeWS):
        def __aiter__(self):
            async def gen():
                for m in self._messages:
                    yield m
                read_done.set()          # reader reached end of feed
                await release.wait()     # keep the connection open
                raise _StopFeed()
            return gen()

    forwarded = []

    async def on_token(m):
        forwarded.append(m)

    fake = SignallingWS([notification(f"s{i}", create_logs(r))
                         for i, r in enumerate(raws)])
    fake_mod = type(sys)("websockets")
    fake_mod.connect = lambda url: fake
    sys.modules["websockets"] = fake_mod
    try:
        await asyncio.wait_for(ws_listener.ws_listen(on_token, reconnect_delay=0.01), 8)
    except (asyncio.TimeoutError, _StopFeed):
        pass
    finally:
        sys.modules.pop("websockets", None)

    check("reader drained the feed while a fetch was pending", read_done.is_set())
    check("all 5 mints still enriched and forwarded",
          sorted(m["mint"] for m in forwarded) == sorted(MINTS),
          f"got {len(forwarded)}")


# ---------------------------------------------------------------- test 2
async def test_kpi_invariant_max_positions():
    print("test 2: KPI invariant with max positions reached")
    reset_bot((True, "ok"))
    # 2 slots, feed 6 distinct tokens: 2 buys then 4 capacity skips
    for i in range(6):
        await snipe.handle_new_token(meta(f"MINT{i}", f"tok{i}"))

    s = tel.stats
    check("scanned == 6", s["scanned"] == 6, f"scanned={s['scanned']}")
    check("buys == max_positions (2)", s["buys"] == 2, f"buys={s['buys']}")
    check("passed + skipped == scanned",
          s["passed"] + s["skipped"] == s["scanned"],
          f"{s['passed']} + {s['skipped']} != {s['scanned']}")
    check("skipped == 4 (capacity)", s["skipped"] == 4, f"skipped={s['skipped']}")


async def test_kpi_invariant_filter_reject():
    print("test 3: KPI invariant when the filter rejects everything")
    reset_bot((False, "mcap too low"))
    for i in range(5):
        await snipe.handle_new_token(meta(f"BAD{i}"))
    s = tel.stats
    check("scanned == 5", s["scanned"] == 5, f"scanned={s['scanned']}")
    check("passed == 0", s["passed"] == 0, f"passed={s['passed']}")
    check("passed + skipped == scanned",
          s["passed"] + s["skipped"] == s["scanned"],
          f"{s['passed']} + {s['skipped']} != {s['scanned']}")
    check("no buys", s["buys"] == 0, f"buys={s['buys']}")


async def test_kpi_invariant_llm_reject():
    print("test 4: KPI invariant when the LLM gate rejects")
    reset_bot((True, "ok"))
    cfg.LLM_ANALYSIS_ENABLED = True

    async def _llm(_m):
        return {"verdict": "reject", "score": 2, "reason": "rugpull vibes"}
    snipe.analyze_token = _llm
    snipe.llm_passed = lambda v: False
    try:
        for i in range(4):
            await snipe.handle_new_token(meta(f"LLM{i}"))
    finally:
        cfg.LLM_ANALYSIS_ENABLED = False

    s = tel.stats
    check("scanned == 4", s["scanned"] == 4, f"scanned={s['scanned']}")
    check("passed + skipped == scanned",
          s["passed"] + s["skipped"] == s["scanned"],
          f"{s['passed']} + {s['skipped']} != {s['scanned']}")
    check("no buys after LLM reject", s["buys"] == 0, f"buys={s['buys']}")


# ---------------------------------------------------------------- test 5
async def test_held_position_not_rescanned():
    print("test 5: re-delivered token already held emits nothing")
    reset_bot((True, "ok"))
    await snipe.handle_new_token(meta("HELD"))
    before = dict(tel.stats)
    feed_before = len(tel.feed)
    for _ in range(10):
        await snipe.handle_new_token(meta("HELD"))
    check("scanned unchanged", tel.stats["scanned"] == before["scanned"],
          f"{before['scanned']} -> {tel.stats['scanned']}")
    check("no extra feed rows", len(tel.feed) == feed_before,
          f"{feed_before} -> {len(tel.feed)}")
    check("buys still 1", tel.stats["buys"] == 1, f"buys={tel.stats['buys']}")


# ---------------------------------------------------------------- tests 6-7
async def test_paper_entry_requires_real_quote():
    """Paper mode must size entries from a REAL Jupiter buy quote. When a mint
    has no route yet the buy returns quote_failed and we must NOT open a
    position - inventing a holding is what produces imaginary P&L."""
    print("test 6: paper entry refused when the token cannot be priced")
    reset_bot((True, "ok"))

    async def _no_route_buy(mint, sol):
        import jupiter
        # exercise the real paper branch with the quote returning None
        jupiter.get_buy_quote = lambda *_a, **_k: _async_none()
        return await jupiter.buy_token(mint, sol)

    async def _async_none():
        return None

    snipe.buy_token = _no_route_buy
    await snipe.handle_new_token(meta("NOROUTE", "Ghost"))

    s = tel.stats
    check("no position opened", "NOROUTE" not in snipe.positions, snipe.positions)
    check("no buy counted", s["buys"] == 0, f"buys={s['buys']}")
    check("counted as a skip, invariant holds",
          s["passed"] + s["skipped"] == s["scanned"] and s["skipped"] == 1,
          f"passed={s['passed']} skipped={s['skipped']} scanned={s['scanned']}")
    check("no P&L recorded", tel.stats["realized_pnl_sol"] == 0.0)


def _install_position(mint="MTM", buy_sol=0.1, tokens=1_000_000):
    """Put a position in place as if a paper buy had just filled."""
    import time as _t
    snipe.positions[mint] = {
        "bought_at": _t.time(), "buy_sol": buy_sol, "token_amount": tokens,
        "paper": True, "meta": {"name": "MarkToMarket"},
    }
    return mint


class CaptureWS:
    """Fake dashboard subscriber that records every broadcast payload.

    Exit events are NOT appended to tel.feed (only token_new is, see
    telemetry.emit), so asserting on the broadcast stream is both the correct
    place to look and the same data a real browser client receives.
    """

    def __init__(self):
        self.events = []

    async def send(self, msg):
        import json
        self.events.append(json.loads(msg))

    def of_type(self, kind):
        return [e for e in self.events if e.get("type") == kind]

    def __enter__(self):
        tel.subscribers.add(self)
        return self

    def __exit__(self, *a):
        tel.subscribers.discard(self)
        return False


async def test_monitor_marks_to_market():
    """TP must fire off a real sell quote, not a random walk. 0.25 SOL back on a
    0.1 SOL entry is 2.5x, above TAKE_PROFIT_MULTIPLE=2.0."""
    print("test 7: monitor exits on a real sell quote (TP)")
    reset_bot((True, "ok"))
    cfg.SELL_DELAY_SEC = 0
    cfg.PRICE_CHECK_SEC = 0.01
    mint = _install_position()

    async def quote(_mint, _amount):
        return 0.25  # 2.5x on a 0.1 SOL entry

    sells = []

    async def _sell(m, amt):
        sells.append((m, amt))
        return {"paper": True, "action": "SELL"}

    snipe.get_sell_quote = quote
    snipe.sell_token = _sell
    with CaptureWS() as cap:
        await asyncio.wait_for(_REAL_MONITOR(mint), 5)

    check("take-profit fired", tel.stats["exits_tp"] == 1, f"tp={tel.stats['exits_tp']}")
    check("no stop-loss", tel.stats["exits_sl"] == 0)
    check("sell was attempted with the held amount",
          sells == [(mint, 1_000_000)], sells)
    check("position closed", mint not in snipe.positions)
    rows = cap.of_type("exit_tp")
    check("exit multiple came from the quote (2.5x)",
          len(rows) == 1 and abs(rows[0]["multiple"] - 2.5) < 1e-6, rows)
    ticks = cap.of_type("position_tick")
    check("tick multiple is quote-derived, not simulated",
          ticks and all(abs(t["multiple"] - 2.5) < 1e-6 for t in ticks), ticks[:2])


async def test_monitor_tolerates_transient_quote_failure():
    """A single failed quote is routine (API hiccup) and must not be read as a
    price collapse - the position stays open and prices normally next tick."""
    print("test 7b: one failed quote does not close the position")
    reset_bot((True, "ok"))
    cfg.SELL_DELAY_SEC = 0
    cfg.PRICE_CHECK_SEC = 0.01
    mint = _install_position()

    calls = {"n": 0}

    async def flaky_quote(_mint, _amount):
        calls["n"] += 1
        if calls["n"] <= 2:
            return None          # transient failure
        return 0.25              # then a real 2.5x

    async def _sell(m, amt):
        return {"paper": True}

    snipe.get_sell_quote = flaky_quote
    snipe.sell_token = _sell
    await asyncio.wait_for(_REAL_MONITOR(mint), 5)

    check("survived the transient failures", calls["n"] == 3, f"calls={calls['n']}")
    check("exited on take-profit, not stop-loss",
          tel.stats["exits_tp"] == 1 and tel.stats["exits_sl"] == 0,
          f"tp={tel.stats['exits_tp']} sl={tel.stats['exits_sl']}")


async def test_monitor_abandons_unpriceable_position():
    """A position that stays unpriceable (liquidity pulled / rugged) must be
    abandoned as a total loss rather than monitored forever."""
    print("test 7c: persistently unpriceable position is abandoned at 0x")
    reset_bot((True, "ok"))
    cfg.SELL_DELAY_SEC = 0
    cfg.PRICE_CHECK_SEC = 0.01
    mint = _install_position()

    async def dead_quote(_mint, _amount):
        return None

    snipe.get_sell_quote = dead_quote
    with CaptureWS() as cap:
        # Bounded: an unpriceable position must be abandoned, not monitored
        # forever. A timeout here IS the failure, so report it as one rather
        # than letting it escape as a traceback.
        try:
            await asyncio.wait_for(_REAL_MONITOR(mint), 5)
        except asyncio.TimeoutError:
            snipe.positions.pop(mint, None)
            check("position abandoned instead of monitored forever", False,
                  "monitor never exited")

    check("position abandoned", mint not in snipe.positions)
    check("recorded as a loss", tel.stats["exits_sl"] == 1, f"sl={tel.stats['exits_sl']}")
    rows = cap.of_type("exit_sl")
    check("marked 0x with an 'unpriceable' reason",
          len(rows) == 1 and rows[0]["multiple"] == 0.0
          and rows[0].get("reason") == "unpriceable", rows)
    check("no phantom exit before the failure budget was spent",
          not cap.of_type("position_tick"), cap.of_type("position_tick")[:2])


async def main():
    await test_ws_dedup()
    await test_ws_fallback_rpc()
    await test_known_event_beside_unknown_blob_is_not_a_launch()
    await test_enrich_retries_unindexed_mint()
    await test_reader_not_blocked_by_slow_enrichment()
    await test_kpi_invariant_max_positions()
    await test_kpi_invariant_filter_reject()
    await test_kpi_invariant_llm_reject()
    await test_held_position_not_rescanned()
    await test_paper_entry_requires_real_quote()
    await test_monitor_marks_to_market()
    await test_monitor_tolerates_transient_quote_failure()
    await test_monitor_abandons_unpriceable_position()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
