"""
test_dashboard_fixes.py - Verifies the three dashboard/telemetry fixes:

  1. ws_listener dedup: the same mint / signature is only forwarded once, and
     non-create transactions never trigger a getTransaction RPC call.
  2. KPI invariant: passed + skipped == scanned (exactly one token_eval verdict
     per token_new), including the max-positions and LLM-reject paths.
  3. Re-delivered tokens already held as positions emit no telemetry at all.

Pure unit tests - no network, no RPC, no real trades. Run:
    .venv/Scripts/python.exe test_dashboard_fixes.py
"""
import asyncio
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


def notification(sig, logs):
    import json
    return json.dumps({
        "method": "logsNotification",
        "params": {"result": {"value": {"signature": sig, "logs": logs}}},
    })


async def test_ws_dedup():
    print("test 1: ws_listener dedup + create-filter")
    CREATE = ["Program log: Instruction: Create"]
    TRADE = ["Program log: Instruction: Buy"]
    msgs = [
        notification("sig1", CREATE),   # new mint  -> forwarded
        notification("sig1", CREATE),   # same sig  -> dropped (no RPC)
        notification("sig2", CREATE),   # new sig, same mint -> dropped at mint level
        notification("sig3", TRADE),    # a plain trade -> dropped, no RPC
        notification("sig4", CREATE),   # new mint  -> forwarded
    ]

    rpc_calls = []
    sig_to_mint = {"sig1": "MINT_A", "sig2": "MINT_A", "sig4": "MINT_B"}

    async def fake_get_tx(sig):
        rpc_calls.append(sig)
        return {"_sig": sig}

    def fake_extract(tx):
        return sig_to_mint.get(tx["_sig"]) if tx else None

    async def fake_meta(mint):
        return meta(mint)

    ws_listener._get_transaction = fake_get_tx
    ws_listener._extract_mint = fake_extract
    ws_listener._fetch_token_meta = fake_meta

    forwarded = []

    async def on_token(m):
        forwarded.append(m["mint"])

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

    check("each mint forwarded exactly once", forwarded == ["MINT_A", "MINT_B"],
          f"got {forwarded}")
    check("duplicate signature makes no RPC call", rpc_calls.count("sig1") == 1,
          f"rpc_calls={rpc_calls}")
    check("non-create tx makes no RPC call", "sig3" not in rpc_calls,
          f"rpc_calls={rpc_calls}")
    check("RPC calls == unique create sigs (3)", len(rpc_calls) == 3,
          f"rpc_calls={rpc_calls}")


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


async def main():
    await test_ws_dedup()
    await test_kpi_invariant_max_positions()
    await test_kpi_invariant_filter_reject()
    await test_kpi_invariant_llm_reject()
    await test_held_position_not_rescanned()
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {FAILURES}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
