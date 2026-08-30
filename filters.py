"""
filters.py - Decide whether a freshly detected token is worth sniping.

We keep this cheap and on-chain-only so it runs fast. Heavy LLM analysis
(Claude Opus) is OUT of scope here; this is the raw safety/quality gate.
"""
import time

import httpx
from config import cfg

# pump.fun bonding curve program - a token is "pre-graduation" while it still
# lives on the curve. We approximate by checking the listing metadata flags.
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


async def get_mint_account_info(mint: str) -> dict:
    """Fetch mint account data to read mint/freeze authority."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [mint, {"encoding": "base64"}],
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(cfg.HELIUS_RPC_URL, json=payload)
        r.raise_for_status()
        data = r.json()
    return data


def parse_mint_authorities(account_data_b64: str):
    """
    Parse a SPL Mint account (82 bytes):
      - bytes 0-43: mint authority option + pubkey
      - bytes 44-? : supply, decimals, freeze authority option + pubkey
    Returns (mint_authority, freeze_authority) where each is a pubkey str or None.
    """
    import base64
    from solders.pubkey import Pubkey

    raw = base64.b64decode(account_data_b64)
    if len(raw) < 82:
        return (None, None)
    # mint authority: byte 0 = 1 if present, then 32 bytes pubkey
    off = 0
    m_opt = raw[off]
    off += 1
    mint_auth = None
    if m_opt == 1:
        mint_auth = str(Pubkey.from_bytes(raw[off:off + 32]))
    off += 32
    off += 8  # supply (u64)
    off += 1  # decimals
    f_opt = raw[off]
    off += 1
    freeze_auth = None
    if f_opt == 1:
        freeze_auth = str(Pubkey.from_bytes(raw[off:off + 32]))
    return (mint_auth, freeze_auth)


async def evaluate_token(meta: dict) -> tuple[bool, str]:
    """
    meta: a dict from the pump.fun listing API (or normalized equivalent).
    Returns (passed, reason). reason explains the first failure or 'OK'.
    """
    # Socials check
    if cfg.REQUIRE_SOCIALS:
        has_website = bool(meta.get("website"))
        has_twitter = bool(meta.get("twitter"))
        has_telegram = bool(meta.get("telegram"))
        if not (has_website or has_twitter or has_telegram):
            return (False, "no socials")

    # Market cap bounds (pump.fun provides usd_market_cap)
    mcap = float(meta.get("usd_market_cap") or 0)
    if mcap < cfg.MIN_MARKET_CAP_USD:
        return (False, f"mcap {mcap:.0f} < min {cfg.MIN_MARKET_CAP_USD:.0f}")
    if mcap > cfg.MAX_MARKET_CAP_USD:
        return (False, f"mcap {mcap:.0f} > max {cfg.MAX_MARKET_CAP_USD:.0f}")

    # Pre-graduation: pump.fun API returns 'status' or we infer from flags.
    # status==0 means still bonding; status==1 means graduated.
    status = meta.get("status")
    if cfg.ONLY_PRE_GRADUATION and status is not None and int(status) != 0:
        return (False, "already graduated")

    # On-chain authority checks (renounced = safe from mint/freeze scams)
    mint = meta.get("mint")
    if cfg.REQUIRE_MINT_RENOUNCED or cfg.REQUIRE_FREEZE_RENOUNCED:
        if not mint:
            return (False, "no mint address")
        try:
            info = await get_mint_account_info(mint)
            res = info.get("result", {})
            acc = res.get("value")
            if not acc:
                return (False, "mint account not found")
            m_auth, f_auth = parse_mint_authorities(acc["data"][0])
            # Some pump tokens store authority as the bonding curve (not None).
            # True renounce => None. We treat curve-owned as NOT renounced for safety.
            if cfg.REQUIRE_MINT_RENOUNCED and m_auth is not None:
                return (False, "mint authority NOT renounced")
            if cfg.REQUIRE_FREEZE_RENOUNCED and f_auth is not None:
                return (False, "freeze authority NOT renounced")
        except Exception as e:
            return (False, f"authority check error: {e}")

    # --- Advanced quality filters (reduce rug exposure) ---
    liq = float(meta.get("liquidity_usd") or 0)
    if cfg.MIN_LIQUIDITY_USD and liq < cfg.MIN_LIQUIDITY_USD:
        return (False, f"liquidity {liq:.0f} < min {cfg.MIN_LIQUIDITY_USD:.0f}")

    txns = int(meta.get("txns_24h") or 0)
    if cfg.MIN_TXNS_24H and txns < cfg.MIN_TXNS_24H:
        return (False, f"txns_24h {txns} < min {cfg.MIN_TXNS_24H}")

    pchg = float(meta.get("price_change_h1") or 0)
    if cfg.MIN_PRICE_CHANGE_H1_PCT and pchg < cfg.MIN_PRICE_CHANGE_H1_PCT:
        return (False, f"price h1 {pchg:.1f}% < min {cfg.MIN_PRICE_CHANGE_H1_PCT:.1f}%")

    created = int(meta.get("pair_created_at") or 0)
    if cfg.MIN_PAIR_AGE_SEC and created:
        age = (int(time.time() * 1000) - created) / 1000.0
        if age < cfg.MIN_PAIR_AGE_SEC:
            return (False, f"pair too new ({age:.0f}s < {cfg.MIN_PAIR_AGE_SEC}s)")

    return (True, "OK")
