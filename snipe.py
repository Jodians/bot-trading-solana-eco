"""
snipe.py - Orchestrator / main entry point.

Flow:
  1. Discover new tokens (WebSocket if USE_WEBSOCKET, else poll pump.fun).
  2. For each new token, run on-chain filters + optional LLM quality gate.
  3. If it passes, buy it (paper DRY-RUN by default) and open a tracked position.
  4. A monitor task checks price via Jupiter sell-quote every PRICE_CHECK_SEC and
     exits the position on TAKE_PROFIT_MULTIPLE (sell) or STOP_LOSS_MULTIPLE (cut).

By default LIVE_TRADING=false -> buys/sells are paper/dry-run. No real funds
move. Read config.py / .env.example before enabling live trading.
"""
import asyncio
import time
from datetime import datetime

from config import cfg
from filters import evaluate_token
from pumpfun_listener import poll_loop, fetch_token_meta
from jupiter import buy_token, sell_token, get_sell_quote
from llm_analysis import analyze_token, passed as llm_passed
from ws_listener import ws_listen
from telegram_notify import notify, enabled as tg_enabled, notify_exit_pnl
from telemetry import tel
from wallet import get_balance_sol
import positions_store

# In-memory position store: mint -> {bought_at, buy_sol, token_amount, meta}
# Mirrored to disk on every mutation (see _persist) so a restart can re-attach
# monitors to tokens the wallet actually holds.
positions = {}
# Guard so a position is only being monitored by one task.
_monitors = set()

# How many consecutive unpriceable checks before we abandon a position. A single
# failed quote is routine (API hiccup, momentary no-route); a long run of them
# means liquidity is gone. At PRICE_CHECK_SEC=10 this is ~1 minute of silence.
MAX_QUOTE_FAILURES = 6


def _persist():
    """Mirror the live position map to disk. Cheap: a handful of small rows."""
    positions_store.save(positions)


async def _has_funds_for_buy() -> tuple[bool, str]:
    """
    Live-only: confirm the wallet can afford BUY_AMOUNT_SOL and still hold back
    MIN_SOL_RESERVE for fees, rent and the eventual sell.

    Paper mode short-circuits to True - no funds move, and requiring a wallet
    would break dry-runs on machines with no key. An unknown balance is treated
    as insufficient: spending against an unverified balance is how a bot buys
    something it cannot afford to exit.
    """
    if not cfg.LIVE_TRADING:
        return True, "paper"
    balance = await get_balance_sol()
    if balance is None:
        return False, "wallet balance unknown (RPC failed) - refusing to buy"
    needed = cfg.BUY_AMOUNT_SOL + cfg.MIN_SOL_RESERVE
    if balance < needed:
        return False, (f"insufficient SOL: have {balance:.4f}, need "
                       f"{cfg.BUY_AMOUNT_SOL:.4f} + {cfg.MIN_SOL_RESERVE:.4f} reserve")
    return True, f"balance {balance:.4f} SOL"


async def handle_new_token(meta: dict):
    mint = meta.get("mint")
    name = meta.get("name", "?")

    # Already holding this mint -> nothing to decide. Guard before any telemetry
    # so a re-delivered token cannot inflate the scanned counter or the feed.
    if mint in positions:
        return

    print(f"[{datetime.now():%H:%M:%S}] new token: {name} ({mint})")
    await tel.emit({"type": "token_new", "mint": mint, "name": name,
                    "symbol": meta.get("symbol", ""), "mcap": meta.get("usd_market_cap", 0)})

    passed, reason = await evaluate_token(meta)
    if not passed:
        print(f"    -> SKIP ({reason})")
        await tel.emit({"type": "token_eval", "mint": mint, "name": name,
                        "passed": False, "reason": reason})
        return

    # Capacity check BEFORE the LLM gate: if we cannot open a position anyway,
    # there is no point paying for an LLM call. Emitting the skip here also
    # keeps the KPI arithmetic honest (exactly one verdict per token, so
    # passed + skipped == scanned).
    if len(positions) >= cfg.MAX_OPEN_POSITIONS:
        print("    -> SKIP (max positions reached)")
        await tel.emit({"type": "token_eval", "mint": mint, "name": name,
                        "passed": False, "reason": "max positions reached"})
        return

    # Funds check BEFORE the LLM gate, for the same reason as the capacity
    # check: no point paying for analysis we cannot act on.
    funded, funds_note = await _has_funds_for_buy()
    if not funded:
        print(f"    -> SKIP ({funds_note})")
        await tel.emit({"type": "token_eval", "mint": mint, "name": name,
                        "passed": False, "reason": funds_note})
        return

    # Optional LLM quality gate (Conduit). Fail-safe: error => PASS (no buy).
    if cfg.LLM_ANALYSIS_ENABLED:
        verdict = await analyze_token(meta)
        print(f"    -> LLM verdict: {verdict['verdict']} score={verdict['score']} ({verdict['reason']})")
        await tel.emit({"type": "llm", "mint": mint, "name": name,
                       "verdict": verdict.get("verdict", "?"),
                       "score": verdict.get("score", 0),
                       "reason": verdict.get("reason", "")})
        if not llm_passed(verdict):
            print("    -> SKIP (LLM rejected)")
            await tel.emit({"type": "token_eval", "mint": mint, "name": name,
                            "passed": False, "reason": f"LLM rejected: {verdict.get('reason', '')}"})
            if tg_enabled():
                notify(f"🚫 <b>SKIP (LLM)</b> {name}\n{verdict['reason']}")
            return

    print(f"    -> PASS ({reason}) -> attempting buy (paper={not cfg.LIVE_TRADING})")
    result = await buy_token(mint, cfg.BUY_AMOUNT_SOL)
    token_amount = result.get("token_amount", 0)

    # A paper buy is priced off a REAL Jupiter quote. If there is no route yet
    # we must not invent a holding - an imaginary position would produce
    # imaginary P&L, which is the whole thing we are trying to avoid.
    if result.get("quote_failed") or token_amount <= 0:
        print("    -> SKIP (no Jupiter route/liquidity yet - cannot price entry)")
        await tel.emit({"type": "token_eval", "mint": mint, "name": name,
                        "passed": False, "reason": "no Jupiter route (unpriceable)"})
        return

    await tel.emit({"type": "token_eval", "mint": mint, "name": name,
                    "passed": True, "reason": reason})

    print(f"    -> buy result: paper={result.get('paper')} tokens={token_amount}")
    if tg_enabled():
        mode = "LIVE" if not result.get("paper") else "PAPER"
        notify(f"✅ <b>BUY ({mode})</b> {name}\n<mute>{mint}</mute>\nSOL: {cfg.BUY_AMOUNT_SOL} | tokens: {token_amount}")
    await tel.emit({"type": "buy", "mint": mint, "name": name,
                    "symbol": meta.get("symbol", ""), "sol": cfg.BUY_AMOUNT_SOL,
                    "paper": result.get("paper", True), "tokens": token_amount})

    positions[mint] = {
        "bought_at": time.time(),
        "buy_sol": cfg.BUY_AMOUNT_SOL,
        "token_amount": token_amount,
        "paper": result.get("paper", True),
        "meta": meta,
    }
    _persist()

    # Start the TP/SL monitor for this position (idempotent per mint).
    if mint not in _monitors:
        _monitors.add(mint)
        asyncio.create_task(monitor_position(mint))


def decide_exit(multiple: float, held_sec: float = 0.0) -> str:
    """
    Pure exit decision from the current price multiple (sol_out / buy_sol) and
    how long the position has been held.

    Returns "TP", "SL", "TIMEOUT", or "" (hold). TP/SL are checked first so a
    position that hits its target on the same tick it ages out is still booked
    as the outcome that actually happened.
    """
    if multiple >= cfg.TAKE_PROFIT_MULTIPLE:
        return "TP"
    if multiple <= cfg.STOP_LOSS_MULTIPLE:
        return "SL"
    if cfg.MAX_HOLD_SEC and held_sec >= cfg.MAX_HOLD_SEC:
        return "TIMEOUT"
    return ""


def restore_positions() -> dict:
    """
    Reload persisted positions into the live map. Returns what was restored.

    Called at startup before the listener runs: the launcher auto-restarts the
    bot, and without this a restart orphans real holdings with no monitor, so
    TP/SL would never fire again.
    """
    restored = positions_store.load()
    for mint, pos in restored.items():
        positions.setdefault(mint, pos)
    return restored


async def monitor_position(mint: str):
    """
    Mark the position to market via a REAL Jupiter sell-quote and exit on TP/SL.

    Paper and live use the SAME pricing path - quotes are read-only, so paper
    mode is honest about what the market would pay; only the sell submission is
    gated. This replaced a `random.uniform(-0.04, 0.05)` simulated walk whose
    mean drift was +0.5%/tick: over 2000 simulated positions it hit take-profit
    99.9% of the time, and the live run showed 105 TP / 0 SL. That curve
    measured the RNG's bias, not any edge, so it was worse than no curve at all.

    A quote can transiently fail (no route, API hiccup). Those are tolerated;
    only a sustained inability to price the position closes it out, so a blip
    cannot be mistaken for a price collapse.

    A position restored from disk skips SELL_DELAY_SEC: it was bought before the
    restart, so the delay has already elapsed and re-waiting would leave real
    holdings unmonitored for another window.
    """
    if not positions.get(mint, {}).get("restored"):
        await asyncio.sleep(cfg.SELL_DELAY_SEC)
    consecutive_quote_failures = 0
    try:
        while mint in positions:
            pos = positions[mint]
            token_amount = pos.get("token_amount", 0)

            if token_amount <= 0:
                # Should not happen: handle_new_token refuses unpriceable entries.
                print(f"[{datetime.now():%H:%M:%S}] {mint}: no token amount, closing")
                del positions[mint]
                _persist()
                break

            sol_out = await get_sell_quote(mint, token_amount)
            if sol_out is None:
                consecutive_quote_failures += 1
                if consecutive_quote_failures >= MAX_QUOTE_FAILURES:
                    print(f"[{datetime.now():%H:%M:%S}] {mint}: unpriceable for "
                          f"{consecutive_quote_failures} checks (likely rugged / "
                          "liquidity pulled) -> abandoning position")
                    await tel.emit({"type": "exit_sl", "mint": mint,
                                    "name": pos.get("meta", {}).get("name", mint),
                                    "multiple": 0.0, "paper": pos.get("paper", True),
                                    "reason": "unpriceable"})
                    if tg_enabled():
                        notify_exit_pnl(pos.get("meta", {}).get("name", mint), 0.0,
                                        pos.get("buy_sol", 0.0), "SL")
                    del positions[mint]
                    _persist()
                    break
                await asyncio.sleep(cfg.PRICE_CHECK_SEC)
                continue

            consecutive_quote_failures = 0
            multiple = sol_out / pos["buy_sol"] if pos["buy_sol"] else 0.0

            held_sec = time.time() - (pos.get("bought_at") or time.time())
            decision = decide_exit(multiple, held_sec)
            print(f"[{datetime.now():%H:%M:%S}] {mint}: now {multiple:.2f}x (TP {cfg.TAKE_PROFIT_MULTIPLE} / SL {cfg.STOP_LOSS_MULTIPLE}) {decision}")
            await tel.emit({"type": "position_tick", "mint": mint,
                           "name": pos.get("meta", {}).get("name", mint),
                           "multiple": round(multiple, 3), "decision": decision})

            if decision in ("TP", "SL", "TIMEOUT"):
                label = {"TP": "TAKE PROFIT", "SL": "STOP LOSS",
                         "TIMEOUT": f"MAX HOLD {cfg.MAX_HOLD_SEC}s"}[decision]
                print(f"    -> {label} @ {multiple:.2f}x -> selling")
                res = await sell_token(mint, token_amount)
                print(f"    -> sell result: paper={res.get('paper')}")
                # A live sell that never confirmed leaves the tokens in the
                # wallet. Keep the position open and retry rather than booking a
                # fill that did not happen.
                if not res.get("paper", True) and not res.get("confirmed"):
                    print(f"    -> sell NOT confirmed ({res.get('note')}) -> "
                          "keeping position open, will retry")
                    await asyncio.sleep(cfg.PRICE_CHECK_SEC)
                    continue
                # A live fill reports what it actually received; prefer it over
                # the quote so realized P&L reflects the real execution.
                filled = res.get("sol_out")
                if filled and pos.get("buy_sol"):
                    multiple = filled / pos["buy_sol"]
                # A timeout is booked by its realized outcome, not as its own
                # bucket: above 1.0x it is a (small) win, below it is a loss.
                bucket = decision.lower()
                if decision == "TIMEOUT":
                    bucket = "tp" if multiple >= 1.0 else "sl"
                await tel.emit({"type": f"exit_{bucket}", "mint": mint,
                                "name": pos.get("meta", {}).get("name", mint),
                                "multiple": round(multiple, 3),
                                "paper": res.get("paper", True),
                                "reason": "max hold" if decision == "TIMEOUT" else decision})
                if tg_enabled():
                    notify_exit_pnl(pos.get("meta", {}).get("name", mint), multiple,
                                    pos.get("buy_sol", 0.0),
                                    bucket.upper())
                del positions[mint]
                _persist()
            else:
                await asyncio.sleep(cfg.PRICE_CHECK_SEC)
    finally:
        _monitors.discard(mint)


async def main():
    cfg.validate()
    mode = "LIVE" if cfg.LIVE_TRADING else "PAPER / DRY-RUN"
    print("=" * 60)
    print(f" Solana Sniper starting in {mode} mode")
    print(f" Wallet: (see wallet.pubkey_str())")
    print(f" Buy: {cfg.BUY_AMOUNT_SOL} SOL | TP x{cfg.TAKE_PROFIT_MULTIPLE} | SL x{cfg.STOP_LOSS_MULTIPLE} | check {cfg.PRICE_CHECK_SEC}s")
    print(f" Max hold: {str(cfg.MAX_HOLD_SEC) + 's' if cfg.MAX_HOLD_SEC else 'unlimited'}"
          f" | reserve {cfg.MIN_SOL_RESERVE} SOL")
    print("=" * 60)
    if not cfg.LIVE_TRADING:
        print("WARNING: No real trades will be executed (LIVE_TRADING=false).")

    # Re-attach monitors to positions held across a restart BEFORE discovery
    # starts, so held tokens are being priced again as early as possible.
    restored = restore_positions()
    if restored:
        print(f"Restored {len(restored)} open position(s) from "
              f"{positions_store.PATH}:")
        for mint, pos in restored.items():
            age = int(time.time() - (pos.get("bought_at") or time.time()))
            print(f"  - {pos['meta']['name']} ({mint}) held {age}s, "
                  f"{pos['token_amount']} tokens, paper={pos['paper']}")
            await tel.emit({"type": "buy", "mint": mint,
                            "name": pos["meta"]["name"], "symbol": "",
                            "sol": pos["buy_sol"], "paper": pos["paper"],
                            "tokens": pos["token_amount"], "restored": True})
            if mint not in _monitors:
                _monitors.add(mint)
                asyncio.create_task(monitor_position(mint))
        if tg_enabled():
            notify(f"♻️ <b>RESTART</b>\nRe-attached {len(restored)} open "
                   f"position(s) after restart.")

    if cfg.USE_WEBSOCKET:
        if not cfg.HELIUS_API_KEY or cfg.HELIUS_API_KEY.startswith("your_"):
            print("ERROR: USE_WEBSOCKET=true but HELIUS_API_KEY missing. Falling back to polling.")
            cfg.USE_WEBSOCKET = False
        else:
            print("Listener: Helius WebSocket (pump.fun logsSubscribe)")
            await ws_listen(handle_new_token)
            return

    print("Listener: pump.fun polling")
    await poll_loop(handle_new_token, interval_sec=2.0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
