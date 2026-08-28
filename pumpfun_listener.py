"""
pumpfun_listener.py - Poll the pump.fun public listing API for newly created
tokens, newest first. This is a simple, robust polling approach (no websocket
reverse-engineering needed). Poll interval is configurable.
"""
import asyncio
import httpx
from config import cfg


async def fetch_new_tokens(limit: int = 30) -> list[dict]:
    """
    Returns a list of token metadata dicts, newest first.
    Each dict has keys like: mint, name, symbol, website, twitter, telegram,
    usd_market_cap, status, created_timestamp, etc.
    """
    url = f"https://frontend-api.pump.fun/coins?offset=0&limit={limit}&sort=created"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
    # API returns either a list or {"coins": [...]}
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "coins" in data:
        return data["coins"]
    return []


async def poll_loop(on_token, interval_sec: float = 2.0, seen: set = None):
    """
    Continuously poll for new tokens and call on_token(meta) for each unseen one.
    `seen` tracks mint addresses already processed.
    """
    if seen is None:
        seen = set()
    while True:
        try:
            tokens = await fetch_new_tokens()
            for t in tokens:
                mint = t.get("mint")
                if not mint or mint in seen:
                    continue
                seen.add(mint)
                await on_token(t)
        except Exception as e:
            print(f"[listener] error: {e}")
        await asyncio.sleep(interval_sec)
