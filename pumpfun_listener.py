"""
pumpfun_listener.py - Poll the pump.fun public listing API for newly created
tokens, newest first. This is a simple, robust polling approach (no websocket
reverse-engineering needed). Poll interval is configurable.
"""
import asyncio
import re

import httpx
from config import cfg


async def fetch_new_tokens(limit: int = 30) -> list[dict]:
    """
    Returns a list of token metadata dicts, newest first.
    Each dict has keys like: mint, name, symbol, website, twitter, telegram,
    usd_market_cap, status, created_timestamp, etc.
    """
    url = cfg.PUMPFUN_LISTING_URL
    # Honour the caller's limit even though the configured URL carries its own.
    if "limit=" in url and limit != 30:
        url = re.sub(r"limit=\d+", f"limit={limit}", url)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
    # API returns either a list or {"coins": [...]}
    if isinstance(data, list):
        coins = data
    elif isinstance(data, dict) and "coins" in data:
        coins = data["coins"]
    else:
        coins = []
    return [_normalize_pumpfun(c) for c in coins]


def _normalize_pumpfun(t: dict) -> dict:
    """
    Bring a pump.fun v3 payload up to the shape filters.py expects.

    v3 differences from the old frontend-api:
      * no `status` field -> graduation is the `complete` boolean
      * no `telegram` field -> only website/twitter are published
      * market cap may arrive as market_cap_usd instead of usd_market_cap
    """
    if not isinstance(t, dict):
        return t
    if t.get("status") is None and "complete" in t:
        t["status"] = 1 if t.get("complete") else 0
    # v3 omits these keys entirely on some coins; filters.py uses .get() but
    # downstream code and tests are easier to reason about with them present.
    t.setdefault("website", None)
    t.setdefault("twitter", None)
    t.setdefault("telegram", None)
    if not t.get("usd_market_cap") and t.get("market_cap_usd"):
        t["usd_market_cap"] = t["market_cap_usd"]
    return t


async def fetch_token_meta(mint: str) -> dict | None:
    """
    Fetch metadata for a single mint.
    Primary: pump.fun public API (currently Cloudflare-blocked -> 530).
    Fallback: DexScreener (public, bot-friendly) for marketCap + socials.
    Returns a dict normalized to the shape filters.py expects:
        mint, name, symbol, website, twitter, telegram, usd_market_cap, status
    or None.
    """
    # --- Primary: pump.fun (v3 host; old host is Cloudflare-blocked) ---
    try:
        url = f"{cfg.PUMPFUN_COIN_URL}/{mint}"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 404:
                return None
            if r.status_code < 500:
                r.raise_for_status()
                return _normalize_pumpfun(r.json())
            # 5xx (Cloudflare 530 etc) -> fall through to DexScreener
    except Exception:
        pass

    # --- Fallback: DexScreener (no auth, not Cloudflare-blocked) ---
    try:
        ds_url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(ds_url)
            r.raise_for_status()
            data = r.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return None
        # pick the most-liquid pair
        pair = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
        bt = pair.get("baseToken", {})
        info = pair.get("info") or {}
        websites = info.get("websites") or []
        socials = info.get("socials") or []
        twitter = next((s.get("url") for s in socials if s.get("type") == "twitter"), None)
        telegram = next((s.get("url") for s in socials if s.get("type") == "telegram"), None)
        return {
            "mint": bt.get("address", mint),
            "name": bt.get("name", ""),
            "symbol": bt.get("symbol", ""),
            "website": websites[0].get("url") if websites else None,
            "twitter": twitter,
            "telegram": telegram,
            "usd_market_cap": pair.get("marketCap") or pair.get("fdv") or 0,
            # DexScreener has no pre-graduation flag; assume still on curve (0)
            "status": 0,
            # --- Extended fields for advanced filters ---
            "liquidity_usd": (pair.get("liquidity") or {}).get("usd", 0) or 0,
            "txns_24h": (pair.get("txns") or {}).get("h24", {}).get("buys", 0)
            + (pair.get("txns") or {}).get("h24", {}).get("sells", 0),
            "txns_h1": (pair.get("txns") or {}).get("h1", {}).get("buys", 0)
            + (pair.get("txns") or {}).get("h1", {}).get("sells", 0),
            "price_change_h1": (pair.get("priceChange") or {}).get("h1", 0) or 0,
            "pair_created_at": pair.get("pairCreatedAt", 0),
        }
    except Exception:
        return None


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
