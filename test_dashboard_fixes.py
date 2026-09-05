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
  8. Live-safety gates: positions survive a restart, a flatlining position ages
     out, a live buy is refused unless the wallet is verifiably funded, and an
     unconfirmed live swap is never booked as a fill.

Pure unit tests - no network, no RPC, no real trades. Run:
    .venv/Scripts/python.exe test_dashboard_fixes.py
"""
import asyncio
import os
import sys
import tempfile

import config

# --- deterministic config for the tests -----------------------------------
cfg = config.cfg
cfg.LIVE_TRADING = False
cfg.MAX_OPEN_POSITIONS = 2
cfg.BUY_AMOUNT_SOL = 0.1
cfg.LLM_ANALYSIS_ENABLED = False
cfg.MAX_HOLD_SEC = 0
cfg.MIN_SOL_RESERVE = 0.02

import positions_store

# Never touch the real positions.json: these tests would otherwise clobber the
# live bot's persisted holdings on the same machine.
positions_store.PATH = os.path.join(tempfile.gettempdir(),
                                    "hermes-test-positions.json")

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


# ------------------------------------------------- test 8: live-safety gates
# These cover the four gaps that made LIVE trading unsafe. Every network call is
# stubbed; cfg.LIVE_TRADING is forced True only inside the stubbed live tests and
# always restored, so nothing here can reach the chain.

def _clear_store():
    """Remove the temp positions file so each test starts from a known state."""
    if os.path.exists(positions_store.PATH):
        os.unlink(positions_store.PATH)


async def test_positions_survive_restart():
    """A restart must not orphan a real holding: the position is persisted on
    entry and reload puts it back with enough detail to sell it."""
    print("test 8: an open position survives a restart")
    reset_bot((True, "ok"))
    _clear_store()
    await snipe.handle_new_token(meta("SURVIVE", "Survivor"))
    check("position opened", "SURVIVE" in snipe.positions, snipe.positions)
    check("persisted to disk", os.path.exists(positions_store.PATH))

    # Simulate the process dying and coming back up.
    snipe.positions.clear()
    snipe._monitors.clear()
    restored = snipe.restore_positions()

    check("position restored", "SURVIVE" in restored, restored)
    p = snipe.positions.get("SURVIVE", {})
    check("sellable amount preserved", p.get("token_amount") == 1000, p)
    check("cost basis preserved", p.get("buy_sol") == cfg.BUY_AMOUNT_SOL, p)
    check("name preserved for alerts", p.get("meta", {}).get("name") == "Survivor", p)
    check("flagged restored so the monitor skips SELL_DELAY",
          p.get("restored") is True, p)
    _clear_store()


async def test_persisted_junk_is_not_restored():
    """Rows that cannot be sold, and a corrupt file, must not resurrect as
    phantom positions the monitor would spin on forever."""
    print("test 8b: unsellable rows and a corrupt file are ignored")
    reset_bot((True, "ok"))
    import json
    with open(positions_store.PATH, "w", encoding="utf-8") as f:
        json.dump({
            "Good": {"bought_at": 1.0, "buy_sol": 0.1, "token_amount": 10, "paper": True},
            "ZeroTok": {"bought_at": 1.0, "buy_sol": 0.1, "token_amount": 0, "paper": True},
            "ZeroSol": {"bought_at": 1.0, "buy_sol": 0, "token_amount": 10, "paper": True},
            "Junk": "not-a-dict",
        }, f)
    loaded = positions_store.load()
    check("sellable row kept", "Good" in loaded, loaded)
    check("zero-token row dropped", "ZeroTok" not in loaded, loaded)
    check("zero-cost row dropped", "ZeroSol" not in loaded, loaded)
    check("non-dict row dropped", "Junk" not in loaded, loaded)

    with open(positions_store.PATH, "w", encoding="utf-8") as f:
        f.write("{ not json at all")
    check("corrupt file yields empty instead of crashing the bot",
          positions_store.load() == {})
    _clear_store()
    check("missing file yields empty", positions_store.load() == {})


async def test_restore_does_not_clobber_live_position():
    """A stale file must never overwrite what the running process already holds."""
    print("test 8c: restore never overwrites a live position")
    reset_bot((True, "ok"))
    import json
    snipe.positions["Held"] = {"bought_at": 5.0, "buy_sol": 0.1,
                               "token_amount": 999, "paper": True,
                               "meta": {"name": "Live"}}
    with open(positions_store.PATH, "w", encoding="utf-8") as f:
        json.dump({"Held": {"bought_at": 1.0, "buy_sol": 0.1, "token_amount": 1,
                            "paper": True, "name": "Stale"}}, f)
    snipe.restore_positions()
    check("in-memory position wins over the file",
          snipe.positions["Held"]["token_amount"] == 999, snipe.positions["Held"])
    _clear_store()


async def test_max_hold_exit_decision():
    """A position that flatlines between TP and SL must age out. Observed in a
    real run: two positions pinned at 0.97x for 45 minutes while 241 later
    candidates were rejected for "max positions reached"."""
    print("test 8d: MAX_HOLD_SEC ages out a flatlining position")
    cfg.MAX_HOLD_SEC = 900
    check("flat 0.97x, young -> hold", snipe.decide_exit(0.97, 100) == "")
    check("flat 0.97x, aged -> TIMEOUT", snipe.decide_exit(0.97, 901) == "TIMEOUT",
          snipe.decide_exit(0.97, 901))
    check("aged exactly at the cap -> TIMEOUT", snipe.decide_exit(0.97, 900) == "TIMEOUT")
    check("TP still wins on an aged position", snipe.decide_exit(2.5, 9999) == "TP")
    check("SL still wins on an aged position", snipe.decide_exit(0.4, 9999) == "SL")
    cfg.MAX_HOLD_SEC = 0
    check("a cap of 0 disables the timeout", snipe.decide_exit(0.97, 999999) == "")


async def test_monitor_sells_aged_position():
    """The decision is only half of it - the monitor must actually sell."""
    print("test 8e: the monitor sells a position that ages out")
    reset_bot((True, "ok"))
    _clear_store()
    import time as _t
    cfg.MAX_HOLD_SEC = 1
    cfg.SELL_DELAY_SEC = 0
    cfg.PRICE_CHECK_SEC = 0.01
    # bought 10s ago and flat at 0.97x: between TP and SL, so only the clock exits
    snipe.positions["Flat"] = {"bought_at": _t.time() - 10, "buy_sol": 0.1,
                               "token_amount": 100, "paper": True,
                               "meta": {"name": "Flatline"}, "restored": True}
    sells = []

    async def quote(_m, _a):
        return 0.097  # 0.97x on a 0.1 SOL entry

    async def sell(m, a):
        sells.append((m, a))
        return {"paper": True, "action": "SELL"}

    snipe.get_sell_quote = quote
    snipe.sell_token = sell
    with CaptureWS() as cap:
        await asyncio.wait_for(_REAL_MONITOR("Flat"), 5)

    check("aged position was sold with the held amount", sells == [("Flat", 100)], sells)
    check("position closed", "Flat" not in snipe.positions)
    check("booked by realized outcome, not its own bucket (0.97x -> loss)",
          tel.stats["exits_sl"] == 1 and tel.stats["exits_tp"] == 0,
          f"sl={tel.stats['exits_sl']} tp={tel.stats['exits_tp']}")
    rows = cap.of_type("exit_sl")
    check("exit carries a 'max hold' reason",
          len(rows) == 1 and rows[0].get("reason") == "max hold", rows)
    check("close is persisted so a restart does not resurrect it",
          positions_store.load() == {}, positions_store.load())
    cfg.MAX_HOLD_SEC = 0


async def test_live_buy_requires_funded_wallet():
    """Spending against an unverified balance is how a bot buys something it
    cannot afford to exit. Unknown balance must count as unfunded."""
    print("test 8f: live buys require a verifiably funded wallet")
    reset_bot((True, "ok"))
    cfg.LIVE_TRADING = False
    ok, note = await snipe._has_funds_for_buy()
    check("paper mode never needs a balance", ok and note == "paper", note)

    cfg.LIVE_TRADING = True
    try:
        async def rich():
            return 5.0
        snipe.get_balance_sol = rich
        ok, note = await snipe._has_funds_for_buy()
        check("funded wallet may buy", ok, note)

        async def thin():
            return 0.11  # needs 0.1 buy + 0.02 reserve = 0.12
        snipe.get_balance_sol = thin
        ok, note = await snipe._has_funds_for_buy()
        check("reserve is enforced, not just the buy size", not ok, note)
        check("refusal quantifies the shortfall", "insufficient SOL" in note, note)

        async def unknown():
            return None
        snipe.get_balance_sol = unknown
        ok, note = await snipe._has_funds_for_buy()
        check("unknown balance counts as unfunded", not ok, note)
        check("refusal names the cause", "unknown" in note.lower(), note)
    finally:
        cfg.LIVE_TRADING = False


async def test_unfunded_wallet_opens_no_position():
    """The gate has to sit in the intake path, not just exist as a helper."""
    print("test 8g: an unfunded wallet opens no position")
    reset_bot((True, "ok"))
    cfg.LIVE_TRADING = True
    try:
        async def broke():
            return 0.0
        bought = []

        async def buy(m, _s):
            bought.append(m)
            return {"paper": False, "token_amount": 1}

        snipe.get_balance_sol = broke
        snipe.buy_token = buy
        await snipe.handle_new_token(meta("POOR", "Poor"))
        check("no buy attempted", bought == [], bought)
        check("no position opened", "POOR" not in snipe.positions, snipe.positions)
        check("counted as skipped, keeping passed+skipped==scanned",
              tel.stats["skipped"] == 1 and tel.stats["passed"] == 0,
              f"skipped={tel.stats['skipped']} passed={tel.stats['passed']}")
    finally:
        cfg.LIVE_TRADING = False


async def test_quote_stays_read_only():
    """Ultra only builds a transaction when the request names a taker. Quotes
    must stay taker-less (no wallet needed, nothing signable); only swaps pass
    one - otherwise the live path dies on a missing `transaction`."""
    print("test 8h: quotes send no taker, swaps do")
    import jupiter
    seen = {}

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"outAmount": "123"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, _url, params=None):
            seen.update(params or {})
            return FakeResp()

    orig = jupiter.httpx.AsyncClient
    jupiter.httpx.AsyncClient = lambda **kw: FakeClient()
    try:
        await jupiter._order("A", "B", 1)
        check("no taker on a plain quote", "taker" not in seen, seen)

        seen.clear()
        cfg.PRIORITY_FEE_LAMPORTS = 7777
        await jupiter._order("A", "B", 1, taker="WALLET")
        check("taker sent when swapping", seen.get("taker") == "WALLET", seen)
        check("priority fee forwarded when configured",
              seen.get("priorityFeeLamports") == "7777", seen)

        seen.clear()
        cfg.PRIORITY_FEE_LAMPORTS = 0
        await jupiter._order("A", "B", 1, taker="WALLET")
        check("priority fee omitted when 0 (Jupiter prices it)",
              "priorityFeeLamports" not in seen, seen)
    finally:
        jupiter.httpx.AsyncClient = orig


async def test_unconfirmed_swap_is_not_a_fill():
    """A submitted tx is not a landed tx. Booking one as a fill leaves the bot
    tracking tokens it may not own."""
    print("test 8i: an unconfirmed live swap is never booked as a fill")
    import jupiter
    cfg.LIVE_TRADING = True
    o_sub, o_ord, o_kp = jupiter._submit, jupiter._order, jupiter.load_keypair
    try:
        jupiter.load_keypair = lambda: type("K", (), {"pubkey": lambda s: "PK"})()

        async def order(*_a, **_kw):
            return {"requestId": "r", "transaction": "t", "outAmount": "500000000"}

        async def unconfirmed(_o):
            return {"confirmed": False, "signature": "SIG",
                    "status": "not confirmed within 45s (last: pending)"}

        jupiter._order = order
        jupiter._submit = unconfirmed

        buy = await jupiter.buy_token("M", 0.1)
        check("unconfirmed buy yields no tokens", buy.get("token_amount") == 0, buy)
        check("unconfirmed buy is flagged so no position opens",
              buy.get("quote_failed") is True, buy)
        check("signature kept for manual inspection", buy.get("signature") == "SIG", buy)

        sell = await jupiter.sell_token("M", 100)
        check("unconfirmed sell reports confirmed=False",
              sell.get("confirmed") is False, sell)
        check("unconfirmed sell claims no proceeds", "sol_out" not in sell, sell)

        async def confirmed(_o):
            return {"confirmed": True, "signature": "SIG2", "status": "confirmed"}

        jupiter._submit = confirmed
        ok = await jupiter.sell_token("M", 100)
        check("confirmed sell books proceeds", ok.get("sol_out") == 0.5, ok)

        # An order with no transaction (fetched without a taker) must be refused
        # rather than raising KeyError on the live path.
        jupiter._submit = o_sub
        res = await jupiter._submit({"requestId": "x"})
        check("order without a transaction is refused, not signed",
              res.get("confirmed") is False
              and "no transaction" in (res.get("error") or ""), res)
    finally:
        jupiter._submit, jupiter._order, jupiter.load_keypair = o_sub, o_ord, o_kp
        cfg.LIVE_TRADING = False


async def test_monitor_retries_unconfirmed_sell():
    """An unconfirmed sell leaves the tokens in the wallet, so the position must
    stay open and retry rather than book a fill that never happened."""
    print("test 8j: an unconfirmed sell keeps the position open to retry")
    reset_bot((True, "ok"))
    _clear_store()
    import time as _t
    cfg.MAX_HOLD_SEC = 0
    cfg.SELL_DELAY_SEC = 0
    cfg.PRICE_CHECK_SEC = 0.01
    snipe.positions["Stuck"] = {"bought_at": _t.time(), "buy_sol": 0.1,
                                "token_amount": 100, "paper": True,
                                "meta": {"name": "Stuck"}, "restored": True}
    attempts = {"n": 0}

    async def quote(_m, _a):
        return 0.25  # 2.5x -> TP on every tick

    async def sell(_m, _a):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return {"paper": False, "confirmed": False, "note": "tx dropped"}
        return {"paper": False, "confirmed": True, "sol_out": 0.25}

    snipe.get_sell_quote = quote
    snipe.sell_token = sell
    await asyncio.wait_for(_REAL_MONITOR("Stuck"), 5)

    check("retried until confirmed", attempts["n"] == 3, attempts)
    check("closed only on the confirmed sell", "Stuck" not in snipe.positions,
          snipe.positions)
    check("exactly one exit booked, not one per attempt",
          tel.stats["exits_tp"] == 1, f"tp={tel.stats['exits_tp']}")
    _clear_store()


async def test_authority_lookup_uses_confirmed_commitment():
    """The bug that produced 2797 of 8957 skips in one live run.

    getAccountInfo without an explicit commitment resolves at "finalized", which
    lags the chain by ~13s. A mint that is seconds old therefore comes back as
    value=None and evaluate_token() reports "mint account not found" - a false
    rejection aimed squarely at the freshest launches. Probed live: every
    pump.fun coin younger than ~15s was NOT-FOUND at finalized and FOUND at both
    confirmed and processed.
    """
    print("test 9: the authority gate reads accounts at a non-lagging commitment")
    import filters

    sent = []

    async def fake_post_rpc(payload, timeout=10.0):
        sent.append(payload)
        return {"result": {"value": None}}

    real_post = filters.post_rpc
    filters.post_rpc = fake_post_rpc
    try:
        await filters.get_mint_account_info("SomeMint1111111111111111111111111111111111")
    finally:
        filters.post_rpc = real_post

    check("one RPC call issued", len(sent) == 1, sent)
    opts = sent[0]["params"][1] if sent else {}
    check("commitment is sent explicitly (not the finalized default)",
          "commitment" in opts, opts)
    check("commitment is confirmed or processed, never finalized",
          opts.get("commitment") in ("confirmed", "processed"), opts)
    check("base64 encoding preserved (the mint parser needs it)",
          opts.get("encoding") == "base64", opts)


async def test_foreign_program_blob_is_not_a_pumpfun_event():
    """The flood that produced 7358 `enrich queue full` lines.

    Solana flattens inner CPI programs into one log list, so a router or fee hook
    can drop its own event blob into a pump.fun transaction. Judging every blob
    in the frame made those look like unrecognised pump.fun events, buying a
    getTransaction call per trade. Attribution by program id fixes it.
    """
    print("test 9b: only pump.fun's own event blobs can trigger the RPC fallback")
    import base64
    import pumpfun_events as E

    other = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"

    # An unrecognised blob emitted by a DIFFERENT program, inside a frame whose
    # only pump.fun blob is a plain TradeEvent.
    foreign = [
        f"Program {E.PUMP_PROGRAM} invoke [1]",
        "Program data: " + base64.b64encode(E.TRADE_EVENT + os.urandom(96)).decode(),
        f"Program {other} invoke [2]",
        "Program data: " + base64.b64encode(os.urandom(48)).decode(),
        f"Program {other} success",
        f"Program {E.PUMP_PROGRAM} success",
    ]
    check("foreign unknown blob does not flag a launch",
          not E.has_unknown_event(foreign))

    # Same shape, but pump.fun has NO recognised event of its own: still not a
    # launch, because the only unknown blob belongs to someone else.
    foreign_only = [
        f"Program {E.PUMP_PROGRAM} invoke [1]",
        f"Program {other} invoke [2]",
        "Program data: " + base64.b64encode(os.urandom(48)).decode(),
        f"Program {other} success",
        f"Program {E.PUMP_PROGRAM} success",
    ]
    check("frame with only foreign event data is not a launch",
          not E.has_unknown_event(foreign_only))

    # An unrecognised blob pump.fun DID emit must still be resolved by RPC:
    # that is the real "program was upgraded" signal.
    pump_unknown = [
        f"Program {E.PUMP_PROGRAM} invoke [1]",
        "Program data: " + base64.b64encode(os.urandom(48)).decode(),
        f"Program {E.PUMP_PROGRAM} success",
    ]
    check("pump.fun's own unknown blob still flags a possible launch",
          E.has_unknown_event(pump_unknown))

    # A real create is still decoded (attribution must not break discovery).
    raw = os.urandom(32)
    ev = E.extract_new_mint(create_logs(raw, "Attr", "ATR"))
    check("CreateEvent still decodes with attribution in place",
          ev is not None and ev["mint"] == E.b58encode(raw), ev)


async def test_maintenance_events_are_recognised():
    """Events pump.fun emits that are not launches must cost zero RPC.

    Sampling live pump.fun transactions found ExtendAccountEvent and
    CloseUserVolumeAccumulatorEvent among the tags KNOWN_EVENTS did not list, so
    83% of event-carrying frames looked "possibly a launch".
    """
    print("test 9c: pump.fun maintenance events cost no RPC")
    import base64
    import pumpfun_events as E

    for name in ("ExtendAccountEvent", "CloseUserVolumeAccumulatorEvent",
                 "SyncUserVolumeAccumulatorEvent", "CompletePumpAmmMigrationEvent"):
        tag = E._discriminator(name)
        check(f"{name} is recognised", tag in E.KNOWN_EVENTS)
        logs = [
            f"Program {E.PUMP_PROGRAM} invoke [1]",
            "Program data: " + base64.b64encode(tag + os.urandom(40)).decode(),
            f"Program {E.PUMP_PROGRAM} success",
        ]
        check(f"{name} frame does not flag a launch", not E.has_unknown_event(logs))
        check(f"{name} is not decoded as a create",
              E.extract_new_mint(logs) is None)

    # The tag observed live 25 times with zero creates, whose name we could not
    # recover. Whitelisted by raw discriminator; assert it stays that way.
    observed = bytes.fromhex("e2d6f62107f293e5")
    check("the observed non-create tag is whitelisted", observed in E.KNOWN_EVENTS)
    check("CreateEvent is not accidentally whitelisted away",
          E.CREATE_EVENT in E.KNOWN_EVENTS
          and E.decode_create_event(observed + os.urandom(40)) is None)


async def test_absent_momentum_field_is_not_zero():
    """The bug that produced 26928 of 27278 skips - a 98.7% rejection rate.

    txns_h1 / liquidity_usd / price_change_h1 come from DexScreener, which has no
    pair for a token seconds old. Probed live: all three were absent on 30 of 30
    consecutive launches while usd_market_cap was present 30/30. The old code read
    `int(meta.get("txns_h1") or 0)`, so every fresh token scored 0 and MIN_TXNS_H1
    rejected it for a reason that had nothing to do with the token.

    An absent field means "unknown" and must not be enforced. A field that IS
    reported must still be enforced, or the gate becomes decoration.
    """
    print("test 10: momentum gates ignore fields the source did not report")
    import filters

    saved = (cfg.MIN_TXNS_H1, cfg.MIN_LIQUIDITY_USD, cfg.MIN_PRICE_CHANGE_H1_PCT,
             cfg.MIN_CURVE_SOL, cfg.MAX_DEV_SHARE_PCT, cfg.MAX_TOP_HOLDER_PCT,
             cfg.MIN_ROUND_TRIP_PCT, cfg.REQUIRE_MINT_RENOUNCED,
             cfg.REQUIRE_FREEZE_RENOUNCED)
    cfg.MIN_TXNS_H1, cfg.MIN_LIQUIDITY_USD, cfg.MIN_PRICE_CHANGE_H1_PCT = 2, 1000, 5
    cfg.MIN_CURVE_SOL = cfg.MAX_DEV_SHARE_PCT = cfg.MAX_TOP_HOLDER_PCT = 0
    cfg.MIN_ROUND_TRIP_PCT = 0
    cfg.REQUIRE_MINT_RENOUNCED = cfg.REQUIRE_FREEZE_RENOUNCED = False
    try:
        base = {"mint": "M" * 43, "usd_market_cap": 5000}
        ok, why = await filters.evaluate_token(dict(base))
        check("a fresh mint with no DexScreener data passes", ok, why)

        ok, why = await filters.evaluate_token(dict(base, txns_h1=1))
        check("a REPORTED low txn count still rejects", not ok, why)
        check("  and says so", "txns_h1 1" in why, why)

        ok, why = await filters.evaluate_token(dict(base, txns_h1=0))
        check("a reported ZERO txn count rejects (0 is data, not absence)",
              not ok, why)

        ok, why = await filters.evaluate_token(dict(base, liquidity_usd=10))
        check("reported thin liquidity still rejects", not ok, why)

        ok, why = await filters.evaluate_token(dict(base, price_change_h1=-20))
        check("reported negative momentum still rejects", not ok, why)

        ok, why = await filters.evaluate_token(dict(base, txns_24h=1))
        check("txns_24h is used when txns_h1 is absent", not ok, why)
    finally:
        (cfg.MIN_TXNS_H1, cfg.MIN_LIQUIDITY_USD, cfg.MIN_PRICE_CHANGE_H1_PCT,
         cfg.MIN_CURVE_SOL, cfg.MAX_DEV_SHARE_PCT, cfg.MAX_TOP_HOLDER_PCT,
         cfg.MIN_ROUND_TRIP_PCT, cfg.REQUIRE_MINT_RENOUNCED,
         cfg.REQUIRE_FREEZE_RENOUNCED) = saved


async def test_curve_sol_gate():
    """MIN_CURVE_SOL replaces MIN_TXNS_H1 as the momentum gate.

    real_sol_reserves is in every pump.fun payload, so unlike txns_h1 it is
    actually readable on a fresh mint - and it predicts exitability: probed over
    20 launches, 8 of 12 tokens below 0.1 SOL had NO Jupiter sell route at all,
    while 8 of 8 at or above 0.1 SOL were sellable.
    """
    print("test 10b: the curve-SOL gate reads real_sol_reserves")
    import filters

    saved = (cfg.MIN_CURVE_SOL, cfg.MIN_TXNS_H1, cfg.MAX_DEV_SHARE_PCT,
             cfg.MAX_TOP_HOLDER_PCT, cfg.MIN_ROUND_TRIP_PCT,
             cfg.REQUIRE_MINT_RENOUNCED, cfg.REQUIRE_FREEZE_RENOUNCED)
    cfg.MIN_CURVE_SOL = 0.1
    cfg.MIN_TXNS_H1 = cfg.MAX_DEV_SHARE_PCT = cfg.MAX_TOP_HOLDER_PCT = 0
    cfg.MIN_ROUND_TRIP_PCT = 0
    cfg.REQUIRE_MINT_RENOUNCED = cfg.REQUIRE_FREEZE_RENOUNCED = False
    try:
        base = {"mint": "M" * 43, "usd_market_cap": 5000}
        check("0.03 SOL in the curve is below the floor",
              filters.curve_sol({"real_sol_reserves": 30_000_000}) == 0.03)
        check("an absent field reads as unknown, not zero",
              filters.curve_sol({}) is None)

        ok, why = await filters.evaluate_token(
            dict(base, real_sol_reserves=30_000_000))
        check("a token with 0.03 SOL in the curve is rejected", not ok, why)
        check("  and the reason names the curve", "curve 0.030 SOL" in why, why)

        ok, why = await filters.evaluate_token(
            dict(base, real_sol_reserves=500_000_000))
        check("a token with 0.5 SOL in the curve passes", ok, why)

        ok, why = await filters.evaluate_token(dict(base))
        check("a meta without the field is not rejected by this gate", ok, why)
    finally:
        (cfg.MIN_CURVE_SOL, cfg.MIN_TXNS_H1, cfg.MAX_DEV_SHARE_PCT,
         cfg.MAX_TOP_HOLDER_PCT, cfg.MIN_ROUND_TRIP_PCT,
         cfg.REQUIRE_MINT_RENOUNCED, cfg.REQUIRE_FREEZE_RENOUNCED) = saved


async def test_dev_share_gate():
    """The anti-rug gate that survives the RPC outage.

    getTokenLargestAccounts is refused by every free endpoint (429/403/400/521/
    402), so MAX_TOP_HOLDER_PCT reports unavailable on essentially every token.
    check_dev_share() asks the same question - can one wallet dump the float? -
    using getTokenAccountBalance, which those endpoints do answer (24 of 25 in a
    live probe at real scan rate).
    """
    print("test 10c: the dev-share gate catches a creator holding the float")
    import filters

    saved = (cfg.MAX_DEV_SHARE_PCT, cfg.RUG_CHECK_FAIL_OPEN)
    cfg.MAX_DEV_SHARE_PCT, cfg.RUG_CHECK_FAIL_OPEN = 40, False

    # Real base58 keys: derive_ata() rejects placeholder strings.
    m = {"mint": "So11111111111111111111111111111111111111112",
         "creator": "11111111111111111111111111111111",
         "token_program": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
         "real_token_reserves": filters.CURVE_INITIAL_TOKENS - 1_000_000}

    def rpc_returning(amount):
        async def _rpc(payload, timeout=10.0):
            return {"result": {"value": {"amount": str(amount)}}}
        return _rpc

    real_post = filters.post_rpc
    try:
        filters.post_rpc = rpc_returning(600_000)          # 60% of sold float
        ok, why = await filters.check_dev_share(m)
        check("a dev holding 60% of the float is rejected", not ok, why)
        check("  and the reason is quantified", "dev holds 60%" in why, why)

        filters.post_rpc = rpc_returning(100_000)          # 10%
        ok, why = await filters.check_dev_share(m)
        check("a dev holding 10% passes", ok, why)

        async def rpc_no_account(payload, timeout=10.0):
            return {"error": {"code": -32602, "message": "could not find account"}}

        filters.post_rpc = rpc_no_account
        ok, why = await filters.check_dev_share(m)
        check("a creator with no token account passes (holds nothing)", ok, why)

        ok, why = await filters.check_dev_share(
            dict(m, real_token_reserves=filters.CURVE_INITIAL_TOKENS))
        check("nothing sold yet -> nothing to dump -> pass", ok, why)

        ok, why = await filters.check_dev_share({"mint": m["mint"]})
        check("a payload without a creator cannot be judged -> pass", ok, why)
    finally:
        filters.post_rpc = real_post
        cfg.MAX_DEV_SHARE_PCT, cfg.RUG_CHECK_FAIL_OPEN = saved


async def test_rug_check_fails_closed():
    """An unreachable RPC must not silently disable the anti-rug layer.

    The old code caught the exception, printed "holder check unavailable" and
    returned PASS. With every free endpoint refusing the call, that turned
    MAX_TOP_HOLDER_PCT into decoration: one live run logged the message on
    essentially every token and recorded zero holder rejections. Unknown is not
    safe, so the default is now fail-closed.
    """
    print("test 10d: an unreachable rug check skips the token, not the check")
    import filters
    import httpx

    saved = (cfg.MAX_TOP_HOLDER_PCT, cfg.MAX_DEV_SHARE_PCT,
             cfg.RUG_CHECK_FAIL_OPEN)
    cfg.MAX_TOP_HOLDER_PCT, cfg.MAX_DEV_SHARE_PCT = 60, 40

    async def rpc_down(payload, timeout=10.0):
        raise httpx.ConnectError("all endpoints refused")

    real_post = filters.post_rpc
    filters.post_rpc = rpc_down
    try:
        cfg.RUG_CHECK_FAIL_OPEN = False
        ok, why = await filters.check_holder_concentration("M" * 43)
        check("holder check fails CLOSED by default", not ok, why)
        check("  and names the real exception type", "ConnectError" in why, why)

        ok, why = await filters.check_dev_share({
            "mint": "So11111111111111111111111111111111111111112",
            "creator": "11111111111111111111111111111111",
            "token_program": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "real_token_reserves": filters.CURVE_INITIAL_TOKENS - 1_000_000})
        check("dev-share check fails CLOSED by default", not ok, why)

        cfg.RUG_CHECK_FAIL_OPEN = True
        ok, _ = await filters.check_holder_concentration("M" * 43)
        check("RUG_CHECK_FAIL_OPEN=true restores the old permissive path", ok)
    finally:
        filters.post_rpc = real_post
        (cfg.MAX_TOP_HOLDER_PCT, cfg.MAX_DEV_SHARE_PCT,
         cfg.RUG_CHECK_FAIL_OPEN) = saved


async def test_free_gates_run_before_paid_ones():
    """Gate order is a budget decision, not cosmetics.

    A token rejected on the listing payload must not first spend an RPC call and
    two Jupiter quotes. With 27k tokens per run that ordering is the difference
    between a working scanner and a rate-limited one.
    """
    print("test 10e: a payload-rejectable token spends no network calls")
    import filters

    saved = (cfg.MAX_MARKET_CAP_USD, cfg.MIN_CURVE_SOL, cfg.MAX_DEV_SHARE_PCT,
             cfg.MAX_TOP_HOLDER_PCT, cfg.MIN_ROUND_TRIP_PCT,
             cfg.REQUIRE_MINT_RENOUNCED, cfg.REQUIRE_FREEZE_RENOUNCED)
    cfg.MAX_MARKET_CAP_USD, cfg.MIN_CURVE_SOL = 30000, 0.1
    cfg.MAX_DEV_SHARE_PCT = cfg.MAX_TOP_HOLDER_PCT = 60
    cfg.MIN_ROUND_TRIP_PCT = 80
    cfg.REQUIRE_MINT_RENOUNCED = cfg.REQUIRE_FREEZE_RENOUNCED = True

    calls = []

    async def counting_rpc(payload, timeout=10.0):
        calls.append(payload.get("method"))
        return {"result": {"value": None}}

    real_post = filters.post_rpc
    filters.post_rpc = counting_rpc
    try:
        ok, why = await filters.evaluate_token(
            {"mint": "M" * 43, "usd_market_cap": 99999,
             "real_sol_reserves": 500_000_000})
        check("an over-cap token is rejected", not ok, why)
        check("  without any RPC call", calls == [], calls)

        ok, why = await filters.evaluate_token(
            {"mint": "M" * 43, "usd_market_cap": 5000,
             "real_sol_reserves": 1_000_000})
        check("a thin-curve token is rejected", not ok, why)
        check("  also without any RPC call", calls == [], calls)
    finally:
        filters.post_rpc = real_post
        (cfg.MAX_MARKET_CAP_USD, cfg.MIN_CURVE_SOL, cfg.MAX_DEV_SHARE_PCT,
         cfg.MAX_TOP_HOLDER_PCT, cfg.MIN_ROUND_TRIP_PCT,
         cfg.REQUIRE_MINT_RENOUNCED, cfg.REQUIRE_FREEZE_RENOUNCED) = saved


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
    await test_positions_survive_restart()
    await test_persisted_junk_is_not_restored()
    await test_restore_does_not_clobber_live_position()
    await test_max_hold_exit_decision()
    await test_monitor_sells_aged_position()
    await test_live_buy_requires_funded_wallet()
    await test_unfunded_wallet_opens_no_position()
    await test_quote_stays_read_only()
    await test_unconfirmed_swap_is_not_a_fill()
    await test_monitor_retries_unconfirmed_sell()
    await test_authority_lookup_uses_confirmed_commitment()
    await test_foreign_program_blob_is_not_a_pumpfun_event()
    await test_maintenance_events_are_recognised()
    await test_absent_momentum_field_is_not_zero()
    await test_curve_sol_gate()
    await test_dev_share_gate()
    await test_rug_check_fails_closed()
    await test_free_gates_run_before_paid_ones()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
