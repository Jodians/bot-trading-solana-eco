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

    txns = int(meta.get("txns_h1") or meta.get("txns_24h") or 0)
    if cfg.MIN_TXNS_H1 and txns < cfg.MIN_TXNS_H1:
        return (False, f"txns_h1 {txns} < min {cfg.MIN_TXNS_H1}")

    pchg = float(meta.get("price_change_h1") or 0)
    if cfg.MIN_PRICE_CHANGE_H1_PCT and pchg < cfg.MIN_PRICE_CHANGE_H1_PCT:
        return (False, f"price h1 {pchg:.1f}% < min {cfg.MIN_PRICE_CHANGE_H1_PCT:.1f}%")

    created = int(meta.get("pair_created_at") or 0)
    if cfg.MIN_PAIR_AGE_SEC and created:
        age = (int(time.time() * 1000) - created) / 1000.0
        if age < cfg.MIN_PAIR_AGE_SEC:
            return (False, f"pair too new ({age:.0f}s < {cfg.MIN_PAIR_AGE_SEC}s)")

    # --- Anti-rug: holder concentration -------------------------------------
    if cfg.MAX_TOP_HOLDER_PCT and mint:
        ok, why = await check_holder_concentration(mint)
        if not ok:
            return (False, why)

    # --- Anti-rug: is the token actually SELLABLE? --------------------------
    # The deadliest failure is not a slow bleed, it is a token you can buy and
    # never sell (honeypot, or liquidity so thin the exit is worthless). Both
    # look identical on paper until you try to exit. So probe the exit BEFORE
    # entering: quote a buy, then quote selling that exact amount straight back.
    if cfg.MIN_ROUND_TRIP_PCT and mint:
        ok, why = await check_round_trip(mint)
        if not ok:
            return (False, why)

    return (True, "OK")


async def check_holder_concentration(mint: str) -> tuple[bool, str]:
    """Reject a token whose supply is concentrated in one non-curve wallet.

    A single wallet holding a large share of supply is the classic pump.fun rug
    setup: the dev (or a sniper bundle) can dump the whole float into the curve
    in one transaction. The bonding curve itself is excluded - it legitimately
    holds the unsold supply pre-graduation.

    One RPC call (getTokenLargestAccounts). Fails OPEN on error: a flaky RPC
    should not silently block every entry.
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts",
               "params": [mint, {"commitment": "confirmed"}]}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(cfg.HELIUS_RPC_URL, json=payload)
            r.raise_for_status()
            accounts = r.json().get("result", {}).get("value") or []
    except Exception as e:
        print(f"    [filter] holder check unavailable ({e}) - allowing")
        return (True, "holder check skipped")

    amounts = sorted((float(a.get("uiAmount") or 0) for a in accounts), reverse=True)
    if len(amounts) < 2:
        return (True, "OK")  # only the curve exists yet - nothing to judge

    # amounts[0] is the bonding curve pre-graduation; the real float is the rest.
    float_supply = sum(amounts[1:])
    if float_supply <= 0:
        return (True, "OK")
    top_pct = amounts[1] / float_supply * 100
    if top_pct > cfg.MAX_TOP_HOLDER_PCT:
        return (False, f"top holder {top_pct:.0f}% of float > "
                       f"max {cfg.MAX_TOP_HOLDER_PCT:.0f}%")
    return (True, "OK")


async def check_round_trip(mint: str) -> tuple[bool, str]:
    """Reject a token we could not sell back at a sane price.

    Quotes BUY_AMOUNT_SOL in, then quotes selling the resulting tokens straight
    back out. The ratio is the instantaneous cost of a full round trip: spread +
    both-side price impact + fees. A healthy pump.fun token sits around 88-95%.
    A honeypot returns no sell route at all, which is the single most valuable
    thing this catches - it is unfalsifiable by a fake chart.

    Two read-only quotes, no signing, so it is safe in paper AND live mode.
    """
    from jupiter import get_buy_quote, get_sell_quote

    try:
        tokens = await get_buy_quote(mint, cfg.BUY_AMOUNT_SOL)
    except Exception as e:
        return (False, f"buy route error: {e}")
    if not tokens:
        return (False, "no buy route")

    try:
        sol_back = await get_sell_quote(mint, tokens)
    except Exception as e:
        return (False, f"sell route error: {e}")
    if not sol_back:
        # Buyable but not sellable. Textbook honeypot.
        return (False, "NO SELL ROUTE (honeypot risk)")

    pct = sol_back / cfg.BUY_AMOUNT_SOL * 100
    if pct < cfg.MIN_ROUND_TRIP_PCT:
        return (False, f"round-trip {pct:.0f}% < min {cfg.MIN_ROUND_TRIP_PCT:.0f}% "
                       "(illiquid / high tax)")
    return (True, "OK")
