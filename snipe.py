"""
snipe.py - Orchestrator / main entry point.

Flow:
  1. Poll pump.fun for new tokens.
  2. For each new token, run filters (authorities, socials, mcap).
  3. If it passes, (paper) "buy" it and track the position.
  4. After SELL_DELAY_SEC, (paper) "sell" using TP/SL multiples (price is
     simulated in paper mode since we don't hold real exposure).

By default LIVE_TRADING=false -> everything is paper/dry-run. No real funds
move. Read config.py / .env.example before enabling live trading.
"""
import asyncio
import time
from datetime import datetime

from config import cfg
from filters import evaluate_token
from pumpfun_listener import poll_loop
from jupiter import buy_token, sell_token

# Simple in-memory position store for paper mode.
positions = {}


async def handle_new_token(meta: dict):
    mint = meta.get("mint")
    name = meta.get("name", "?")
    print(f"[{datetime.now():%H:%M:%S}] new token: {name} ({mint})")

    passed, reason = await evaluate_token(meta)
    if not passed:
        print(f"    -> SKIP ({reason})")
        return

    if len(positions) >= cfg.MAX_OPEN_POSITIONS:
        print("    -> SKIP (max positions reached)")
        return

    print(f"    -> PASS ({reason}) -> attempting buy (paper={not cfg.LIVE_TRADING})")
    result = await buy_token(mint, cfg.BUY_AMOUNT_SOL)
    print(f"    -> buy result: {result}")

    positions[mint] = {
        "bought_at": time.time(),
        "buy_sol": cfg.BUY_AMOUNT_SOL,
        "meta": meta,
        "result": result,
    }

    # Schedule a paper sell after delay
    asyncio.create_task(schedule_sell(mint))


async def schedule_sell(mint: str):
    await asyncio.sleep(cfg.SELL_DELAY_SEC)
    pos = positions.get(mint)
    if not pos:
        return
    # In paper mode we don't know real price movement; we simulate a +0% exit
    # so the flow is exercised end-to-end. Replace with real balance/price
    # checks when wiring live trading.
    print(f"[{datetime.now():%H:%M:%S}] sell trigger for {mint} (paper)")
    result = await sell_token(mint, token_amount=0)
    print(f"    -> sell result: {result}")
    positions.pop(mint, None)


async def main():
    cfg.validate()
    mode = "LIVE" if cfg.LIVE_TRADING else "PAPER / DRY-RUN"
    print("=" * 60)
    print(f" Solana Sniper starting in {mode} mode")
    print(f" Wallet: (see wallet.pubkey_str())")
    print(f" Buy size: {cfg.BUY_AMOUNT_SOL} SOL | TP x{cfg.TAKE_PROFIT_MULTIPLE} | SL x{cfg.STOP_LOSS_MULTIPLE}")
    print("=" * 60)
    if not cfg.LIVE_TRADING:
        print("WARNING: No real trades will be executed (LIVE_TRADING=false).")
    await poll_loop(handle_new_token, interval_sec=2.0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
