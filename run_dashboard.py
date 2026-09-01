"""
run_dashboard.py - Launch the live dashboard + the sniper bot in ONE asyncio
process so they share the in-memory telemetry bus.

    python run_dashboard.py

Then open http://localhost:8765

The bot runs exactly as `python snipe.py` would (PAPER by default - no real
funds move). Events flow from snipe.py -> telemetry -> WebSocket -> browser.
"""
import asyncio

from dotenv import load_dotenv

load_dotenv()

from config import cfg
from telemetry import tel
import dashboard_server
import snipe


async def main():
    # Seed dashboard config/state from the bot's config.
    tel.stats["mode"] = "LIVE" if cfg.LIVE_TRADING else "PAPER"
    tel.stats["llm_enabled"] = cfg.LLM_ANALYSIS_ENABLED
    tel.stats["th_tp"] = cfg.TAKE_PROFIT_MULTIPLE
    tel.stats["th_sl"] = cfg.STOP_LOSS_MULTIPLE
    tel.stats["buy_sol"] = cfg.BUY_AMOUNT_SOL
    tel.stats["max_pos"] = cfg.MAX_OPEN_POSITIONS
    tel.stats["min_mcap"] = cfg.MIN_MARKET_CAP_USD
    tel.stats["max_mcap"] = cfg.MAX_MARKET_CAP_USD

    # Seed the P&L equity curve at zero so the chart has a starting point.
    tel.record_equity()

    # Binds 127.0.0.1 by default (see dashboard_server.DEFAULT_HOST). There is
    # no auth on either port and the WS accepts control commands, so exposing
    # it beyond loopback needs a deliberate DASHBOARD_HOST=0.0.0.0.
    host = dashboard_server.DEFAULT_HOST
    server = await dashboard_server.serve_dashboard(host, dashboard_server.WS_PORT)
    print("=" * 60)
    print(f"  Dashboard  : http://127.0.0.1:{dashboard_server.HTTP_PORT}  (mode={tel.stats['mode']})")
    print(f"  WS stream   : ws://127.0.0.1:{dashboard_server.WS_PORT}  (bind={host})")
    print(f"  Bot        : snipe.py (LLM gate={'on' if cfg.LLM_ANALYSIS_ENABLED else 'off'})")
    print(f"  Pricing    : real Jupiter quotes (slippage {cfg.SLIPPAGE_BPS} bps)")
    print("=" * 60)

    # Run the bot loop as a background task. It runs forever (poll/WS loop).
    bot_task = asyncio.create_task(snipe.main())
    try:
        await bot_task
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        server[0].close()
        server[1].shutdown()


if __name__ == "__main__":
    asyncio.run(main())
