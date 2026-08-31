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
import random
import time
from datetime import datetime

from config import cfg
from filters import evaluate_token
from pumpfun_listener import poll_loop, fetch_token_meta
from jupiter import buy_token, sell_token, get_sell_quote
from llm_analysis import analyze_token, passed as llm_passed
from ws_listener import ws_listen
from telegram_notify import notify, enabled as tg_enabled
from telemetry import tel

# In-memory position store: mint -> {bought_at, buy_sol, token_amount, meta}
positions = {}
# Guard so a position is only being monitored by one task.
_monitors = set()


async def handle_new_token(meta: dict):
    mint = meta.get("mint")
    name = meta.get("name", "?")
    print(f"[{datetime.now():%H:%M:%S}] new token: {name} ({mint})")
    await tel.emit({"type": "token_new", "mint": mint, "name": name,
                    "symbol": meta.get("symbol", ""), "mcap": meta.get("usd_market_cap", 0)})

    passed, reason = await evaluate_token(meta)
    if not passed:
        print(f"    -> SKIP ({reason})")
        await tel.emit({"type": "token_eval", "mint": mint, "name": name,
                        "passed": False, "reason": reason})
        return
    await tel.emit({"type": "token_eval", "mint": mint, "name": name,
                    "passed": True, "reason": reason})

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

    if len(positions) >= cfg.MAX_OPEN_POSITIONS:
        print("    -> SKIP (max positions reached)")
        await tel.emit({"type": "token_eval", "mint": mint, "name": name,
                        "passed": False, "reason": "max positions reached"})
        return

    print(f"    -> PASS ({reason}) -> attempting buy (paper={not cfg.LIVE_TRADING})")
    result = await buy_token(mint, cfg.BUY_AMOUNT_SOL)
    token_amount = result.get("token_amount", 0)
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

    # Start the TP/SL monitor for this position (idempotent per mint).
    if mint not in _monitors:
        _monitors.add(mint)
        asyncio.create_task(monitor_position(mint))


def decide_exit(multiple: float) -> str:
    """
    Pure TP/SL decision from the current price multiple (sol_out / buy_sol).
    Returns "TP", "SL", or "" (hold).
    """
    if multiple >= cfg.TAKE_PROFIT_MULTIPLE:
        return "TP"
    if multiple <= cfg.STOP_LOSS_MULTIPLE:
        return "SL"
    return ""


async def monitor_position(mint: str):
    """
    Poll price via Jupiter sell-quote; exit on TP or SL multiple.
    Paper mode: quotes are real (read-only) but sells stay DRY-RUN.
    """
    await asyncio.sleep(cfg.SELL_DELAY_SEC)
    try:
        while mint in positions:
            pos = positions[mint]
            token_amount = pos.get("token_amount", 0)
            is_paper = pos.get("paper", True)

            if is_paper:
                # --- PAPER DRY-RUN: no real on-chain holding, so simulate a
                # price random-walk to exercise TP/SL + the dashboard. This
                # avoids hitting Jupiter with the dummy paper amount (which
                # returns HTTP 400) and lets paper positions actually exit. ---
                if "paper_mult" not in pos:
                    pos["paper_mult"] = 1.0
                drift = random.uniform(-0.04, 0.05)
                pos["paper_mult"] = max(0.05, min(5.0, pos["paper_mult"] + drift))
                multiple = pos["paper_mult"]
            else:
                if token_amount <= 0:
                    # Real holding but amount unknown -> age out after a while.
                    if time.time() - pos["bought_at"] > 300:
                        print(f"[{datetime.now():%H:%M:%S}] {mint}: sim expired, closing")
                        del positions[mint]
                    await asyncio.sleep(cfg.PRICE_CHECK_SEC)
                    continue
                sol_out = await get_sell_quote(mint, token_amount)
                if sol_out is None:
                    await asyncio.sleep(cfg.PRICE_CHECK_SEC)
                    continue
                multiple = sol_out / pos["buy_sol"] if pos["buy_sol"] else 0.0

            decision = decide_exit(multiple)
            print(f"[{datetime.now():%H:%M:%S}] {mint}: now {multiple:.2f}x (TP {cfg.TAKE_PROFIT_MULTIPLE} / SL {cfg.STOP_LOSS_MULTIPLE}) {decision}")
            await tel.emit({"type": "position_tick", "mint": mint,
                           "name": pos.get("meta", {}).get("name", mint),
                           "multiple": round(multiple, 3), "decision": decision})

            if decision == "TP":
                print(f"    -> TAKE PROFIT @ {multiple:.2f}x -> selling")
                res = await sell_token(mint, token_amount)
                print(f"    -> sell result: paper={res.get('paper')}")
                await tel.emit({"type": "exit_tp", "mint": mint, "name": pos.get("meta", {}).get("name", mint), "multiple": round(multiple, 3), "paper": res.get("paper", True)})
                if tg_enabled():
                    notify(f"📈 <b>TAKE PROFIT</b> {pos.get('meta', {}).get('name', mint)} @ {multiple:.2f}x (paper={res.get('paper')})")
                del positions[mint]
            elif decision == "SL":
                print(f"    -> STOP LOSS @ {multiple:.2f}x -> selling (cut)")
                res = await sell_token(mint, token_amount)
                print(f"    -> sell result: paper={res.get('paper')}")
                await tel.emit({"type": "exit_sl", "mint": mint, "name": pos.get("meta", {}).get("name", mint), "multiple": round(multiple, 3), "paper": res.get("paper", True)})
                if tg_enabled():
                    notify(f"📉 <b>STOP LOSS</b> {pos.get('meta', {}).get('name', mint)} @ {multiple:.2f}x (paper={res.get('paper')})")
                del positions[mint]
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
    print("=" * 60)
    if not cfg.LIVE_TRADING:
        print("WARNING: No real trades will be executed (LIVE_TRADING=false).")

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
