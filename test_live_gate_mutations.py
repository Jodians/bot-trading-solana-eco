"""
test_live_gate_mutations.py - do the live-safety checks actually FAIL when the
gate they guard is removed?

test_dashboard_fixes.py proves the four live-trading gates behave correctly.
This proves those assertions are load-bearing: a green test that stays green
against broken code proves nothing. Each mutation monkeypatches ONE gate back to
its pre-fix behaviour, re-runs the relevant assertion, and expects it to notice.

Source files are never modified - every mutation is an in-process monkeypatch,
reverted in a finally block. No network, no RPC, no real trades. Run:
    .venv/Scripts/python.exe test_live_gate_mutations.py

Exit code 0 means every gate is load-bearing; 1 means a mutation slipped past
its check, which means that check is decoration and needs strengthening.
"""
import asyncio
import os
import sys
import tempfile
import time

# Run from the repo regardless of the caller's cwd, without hardcoding a
# machine-specific path.
_REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _REPO)
os.chdir(_REPO)

import config

cfg = config.cfg
cfg.LIVE_TRADING = False
cfg.BUY_AMOUNT_SOL = 0.1
cfg.TAKE_PROFIT_MULTIPLE = 2.0
cfg.STOP_LOSS_MULTIPLE = 0.5
cfg.MIN_SOL_RESERVE = 0.02
cfg.LLM_ANALYSIS_ENABLED = False
cfg.MAX_OPEN_POSITIONS = 3
cfg.SELL_DELAY_SEC = 0
cfg.PRICE_CHECK_SEC = 0.01

import positions_store

# Never touch the running bot's real positions.json.
positions_store.PATH = os.path.join(tempfile.gettempdir(),
                                    "test-mutation-positions.json")

import jupiter
import snipe
from telemetry import tel

# Captured BEFORE any mutation stubs it out. Mutation 2 replaces
# snipe.monitor_position with a no-op to keep token intake off the network; a
# later mutation that called the module attribute would silently exercise the
# stub and report a false MISS. (That exact bug produced one during development.)
_REAL_MONITOR = snipe.monitor_position

RESULTS = []


def expect_caught(name, noticed: bool, detail=""):
    """noticed=True means the assertion spotted the regression - that's a pass."""
    print(f"  {'CAUGHT' if noticed else 'MISSED'}  {name}"
          + ("" if noticed else f"  {detail}"))
    RESULTS.append((name, noticed))


def clean():
    if os.path.exists(positions_store.PATH):
        os.unlink(positions_store.PATH)
    snipe.positions.clear()
    snipe._monitors.clear()
    for k in ("scanned", "passed", "skipped", "buys", "exits_tp", "exits_sl"):
        tel.stats[k] = 0
    snipe.tg_enabled = lambda: False


# --------------------------------------------------------------- mutation 1
def m_no_timeout():
    """Restore the old two-branch decide_exit that only knew TP and SL."""
    print("mutation 1: decide_exit ignores held_sec")
    cfg.MAX_HOLD_SEC = 900

    def old_decide(multiple, held_sec=0.0):
        if multiple >= cfg.TAKE_PROFIT_MULTIPLE:
            return "TP"
        if multiple <= cfg.STOP_LOSS_MULTIPLE:
            return "SL"
        return ""

    real = snipe.decide_exit
    snipe.decide_exit = old_decide
    try:
        got = snipe.decide_exit(0.97, 901)
        expect_caught("aged flat position no longer exits", got != "TIMEOUT", got)
    finally:
        snipe.decide_exit = real


# --------------------------------------------------------------- mutation 2
def m_no_funds_gate():
    """Remove the balance gate: an empty wallet should then buy anyway."""
    print("mutation 2: _has_funds_for_buy always says yes")
    clean()
    cfg.LIVE_TRADING = True
    real_gate = snipe._has_funds_for_buy
    real_monitor = snipe.monitor_position
    try:
        async def always_ok():
            return True, "stubbed"

        async def broke():
            return 0.0

        async def ev(_m):
            return (True, "ok")

        bought = []

        async def buy(m, _s):
            bought.append(m)
            return {"paper": False, "token_amount": 1}

        async def noop_monitor(_m):
            return

        snipe._has_funds_for_buy = always_ok
        snipe.get_balance_sol = broke
        snipe.evaluate_token = ev
        snipe.buy_token = buy
        snipe.monitor_position = noop_monitor
        asyncio.run(snipe.handle_new_token(
            {"mint": "Poor", "name": "Poor", "symbol": "P", "usd_market_cap": 5000}))
        expect_caught("empty wallet now buys anyway", bought == ["Poor"], bought)
    finally:
        snipe._has_funds_for_buy = real_gate
        snipe.monitor_position = real_monitor
        cfg.LIVE_TRADING = False


# --------------------------------------------------------------- mutation 3
def m_no_confirmation():
    """Report a swap as done straight off /execute, with no status check."""
    print("mutation 3: _submit reports success without confirming")
    cfg.LIVE_TRADING = True
    o_sub, o_ord, o_kp = jupiter._submit, jupiter._order, jupiter.load_keypair
    try:
        jupiter.load_keypair = lambda: type("K", (), {"pubkey": lambda s: "PK"})()

        async def order(*_a, **_kw):
            return {"requestId": "r", "transaction": "t", "outAmount": "500000000"}

        async def blind_submit(_o):
            return {"signature": "SIG", "result": {"status": "Success"}}

        jupiter._order = order
        jupiter._submit = blind_submit
        buy = asyncio.run(jupiter.buy_token("M", 0.1))
        sell = asyncio.run(jupiter.sell_token("M", 100))
        # A submit with no `confirmed` key must still be treated as unconfirmed:
        # no tokens booked on the buy, no proceeds claimed on the sell.
        expect_caught("a dropped tx would be booked as a fill",
                      buy.get("token_amount", 0) == 0 and "sol_out" not in sell,
                      f"buy_tokens={buy.get('token_amount')} sell_keys={list(sell)}")
    finally:
        jupiter._submit, jupiter._order, jupiter.load_keypair = o_sub, o_ord, o_kp
        cfg.LIVE_TRADING = False


# --------------------------------------------------------------- mutation 4
def m_no_persist():
    """Make persistence a no-op: a restart should then lose the position."""
    print("mutation 4: _persist is a no-op")
    clean()
    real = snipe._persist
    try:
        snipe._persist = lambda: None
        snipe.positions["MintA"] = {"bought_at": 1.0, "buy_sol": 0.1,
                                    "token_amount": 5, "paper": True,
                                    "meta": {"name": "A"}}
        snipe._persist()
        snipe.positions.clear()          # the process dies
        restored = snipe.restore_positions()
        expect_caught("restart would lose the position", restored == {}, restored)
    finally:
        snipe._persist = real


# --------------------------------------------------------------- mutation 5
def m_restored_rewaits():
    """Drop the `restored` flag: the monitor should then re-wait SELL_DELAY_SEC,
    leaving a real holding unpriced for another window after a restart."""
    print("mutation 5: a restored position re-waits SELL_DELAY_SEC")
    clean()
    cfg.MAX_HOLD_SEC = 0
    cfg.SELL_DELAY_SEC = 3           # long enough to measure
    cfg.PRICE_CHECK_SEC = 0.01
    snipe.positions["Old"] = {"bought_at": time.time() - 100, "buy_sol": 0.1,
                              "token_amount": 100, "paper": True,
                              "meta": {"name": "Old"}}   # no `restored` flag
    first_quote_at = {}

    async def quote(_m, _a):
        first_quote_at.setdefault("t", time.time())
        return 0.25

    async def sell(_m, _a):
        return {"paper": True}

    snipe.get_sell_quote = quote
    snipe.sell_token = sell
    t0 = time.time()
    asyncio.run(asyncio.wait_for(_REAL_MONITOR("Old"), 10))
    delay = first_quote_at.get("t", t0) - t0
    expect_caught("unflagged position left unpriced for SELL_DELAY", delay >= 2.5,
                  f"delay={delay:.2f}s")
    cfg.SELL_DELAY_SEC = 0


# --------------------------------------------------------------- mutation 6
def m_absent_txns_is_zero():
    """Restore `int(meta.get("txns_h1") or 0)`: a fresh mint that reports no
    momentum fields at all should then be rejected for having 0 txns - the bug
    that skipped 26928 of 27278 tokens in one run."""
    print("mutation 6: an absent txns_h1 counts as zero again")
    import filters

    saved = (cfg.MIN_TXNS_H1, cfg.MIN_CURVE_SOL, cfg.MAX_DEV_SHARE_PCT,
             cfg.MAX_TOP_HOLDER_PCT, cfg.MIN_ROUND_TRIP_PCT,
             cfg.REQUIRE_MINT_RENOUNCED, cfg.REQUIRE_FREEZE_RENOUNCED)
    cfg.MIN_TXNS_H1 = 2
    cfg.MIN_CURVE_SOL = cfg.MAX_DEV_SHARE_PCT = cfg.MAX_TOP_HOLDER_PCT = 0
    cfg.MIN_ROUND_TRIP_PCT = 0
    cfg.REQUIRE_MINT_RENOUNCED = cfg.REQUIRE_FREEZE_RENOUNCED = False

    real = filters.evaluate_token

    async def old_eval(meta):
        txns = int(meta.get("txns_h1") or meta.get("txns_24h") or 0)
        if cfg.MIN_TXNS_H1 and txns < cfg.MIN_TXNS_H1:
            return (False, f"txns_h1 {txns} < min {cfg.MIN_TXNS_H1}")
        return await real(meta)

    filters.evaluate_token = old_eval
    try:
        ok, why = asyncio.run(filters.evaluate_token(
            {"mint": "M" * 43, "usd_market_cap": 5000}))
        expect_caught("a fresh mint is skipped for phantom zero txns", not ok, why)
    finally:
        filters.evaluate_token = real
        (cfg.MIN_TXNS_H1, cfg.MIN_CURVE_SOL, cfg.MAX_DEV_SHARE_PCT,
         cfg.MAX_TOP_HOLDER_PCT, cfg.MIN_ROUND_TRIP_PCT,
         cfg.REQUIRE_MINT_RENOUNCED, cfg.REQUIRE_FREEZE_RENOUNCED) = saved


# --------------------------------------------------------------- mutation 7
def m_no_dev_share_gate():
    """Disable MAX_DEV_SHARE_PCT: a creator holding 90% of the sold float - one
    transaction away from dumping it - should then sail through."""
    print("mutation 7: MAX_DEV_SHARE_PCT disabled")
    import filters

    saved = (cfg.MAX_DEV_SHARE_PCT, cfg.MAX_TOP_HOLDER_PCT,
             cfg.MIN_ROUND_TRIP_PCT, cfg.MIN_CURVE_SOL,
             cfg.REQUIRE_MINT_RENOUNCED, cfg.REQUIRE_FREEZE_RENOUNCED)
    cfg.MAX_DEV_SHARE_PCT = 0                     # the mutation
    cfg.MAX_TOP_HOLDER_PCT = cfg.MIN_ROUND_TRIP_PCT = cfg.MIN_CURVE_SOL = 0
    cfg.REQUIRE_MINT_RENOUNCED = cfg.REQUIRE_FREEZE_RENOUNCED = False

    real_post = filters.post_rpc

    async def dev_holds_almost_everything(payload, timeout=10.0):
        return {"result": {"value": {"amount": "900000"}}}

    filters.post_rpc = dev_holds_almost_everything
    try:
        ok, why = asyncio.run(filters.evaluate_token({
            "mint": "So11111111111111111111111111111111111111112",
            "creator": "11111111111111111111111111111111",
            "token_program": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "usd_market_cap": 5000,
            "real_token_reserves": filters.CURVE_INITIAL_TOKENS - 1_000_000}))
        expect_caught("a dev holding 90% of the float now passes", ok, why)
    finally:
        filters.post_rpc = real_post
        (cfg.MAX_DEV_SHARE_PCT, cfg.MAX_TOP_HOLDER_PCT,
         cfg.MIN_ROUND_TRIP_PCT, cfg.MIN_CURVE_SOL,
         cfg.REQUIRE_MINT_RENOUNCED, cfg.REQUIRE_FREEZE_RENOUNCED) = saved


# --------------------------------------------------------------- mutation 8
def m_rug_check_fails_open():
    """Restore fail-open: with every RPC endpoint refusing the call, an unknown
    holder distribution should then be treated as safe - which is how the
    anti-rug layer silently switched itself off in the first place."""
    print("mutation 8: rug checks fail open on RPC failure")
    import filters
    import httpx

    saved = (cfg.MAX_TOP_HOLDER_PCT, cfg.RUG_CHECK_FAIL_OPEN)
    cfg.MAX_TOP_HOLDER_PCT = 60
    cfg.RUG_CHECK_FAIL_OPEN = True                # the mutation

    real_post = filters.post_rpc

    async def rpc_down(payload, timeout=10.0):
        raise httpx.ConnectError("all endpoints refused")

    filters.post_rpc = rpc_down
    try:
        ok, why = asyncio.run(filters.check_holder_concentration("M" * 43))
        expect_caught("an unverifiable token is treated as safe", ok, why)
    finally:
        filters.post_rpc = real_post
        (cfg.MAX_TOP_HOLDER_PCT, cfg.RUG_CHECK_FAIL_OPEN) = saved


if __name__ == "__main__":
    m_no_timeout()
    m_no_funds_gate()
    m_no_confirmation()
    m_no_persist()
    m_restored_rewaits()
    m_absent_txns_is_zero()
    m_no_dev_share_gate()
    m_rug_check_fails_open()
    if os.path.exists(positions_store.PATH):
        os.unlink(positions_store.PATH)
    missed = [n for n, ok in RESULTS if not ok]
    print()
    if missed:
        print(f"{len(missed)} MUTATION(S) NOT CAUGHT: {missed}")
        sys.exit(1)
    print(f"all {len(RESULTS)} mutations caught - the gates are load-bearing")
