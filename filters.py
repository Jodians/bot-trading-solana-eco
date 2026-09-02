"""
filters.py - Decide whether a freshly detected token is worth sniping.

We keep this cheap and on-chain-only so it runs fast. Heavy LLM analysis
(Claude Opus) is OUT of scope here; this is the raw safety/quality gate.

Gate ordering is deliberate: every check that reads the listing payload runs
before any check that costs an RPC call or a Jupiter quote, so a token rejected
on free data never spends network budget.
"""
import time

from config import cfg
from rpc import post_rpc

# pump.fun bonding curve program - a token is "pre-graduation" while it still
# lives on the curve. We approximate by checking the listing metadata flags.
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Associated Token Account program - needed to derive the creator's token account
# from (creator, mint) without an extra lookup.
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"

# Tokens the bonding curve starts with, in base units (6 decimals). The rest of
# the 1B supply is reserved for the post-graduation pool. Sold float =
# CURVE_INITIAL_TOKENS - real_token_reserves.
CURVE_INITIAL_TOKENS = 793_100_000 * 10**6

# Lamports per SOL.
LAMPORTS = 10**9


def curve_sol(meta: dict) -> float | None:
    """SOL currently held by the bonding curve, or None if the source omits it.

    None means "unknown", not "zero". The pump.fun listing/coin payload always
    carries real_sol_reserves, but the DexScreener fallback (used when pump.fun
    5xxs) does not - and for those metas MIN_LIQUIDITY_USD is the equivalent
    gate. Collapsing absent to 0 here would rebuild the exact bug this gate
    replaces.
    """
    raw = meta.get("real_sol_reserves")
    return None if raw is None else float(raw) / LAMPORTS


def _unavailable(what: str, err) -> tuple[bool, str]:
    """Verdict for a rug check that could not reach any RPC endpoint.

    Fail-closed by default: an unknown answer is not a safe answer, and these
    gates exist precisely for the tokens most likely to break them. The old
    fail-open silently disabled the whole anti-rug layer - one run logged
    "holder check unavailable" on essentially every token while reporting no
    rejections at all.
    """
    reason = f"{what} unavailable ({type(err).__name__})"
    if cfg.RUG_CHECK_FAIL_OPEN:
        print(f"    [filter] {reason} - allowing (RUG_CHECK_FAIL_OPEN)")
        return (True, reason)
    return (False, reason)


async def get_mint_account_info(mint: str) -> dict:
    """Fetch mint account data to read mint/freeze authority.

    The commitment level is NOT optional here. At the RPC default (finalized) a
    mint created seconds ago has no account yet, so this returns value=None and
    evaluate_token() reports "mint account not found" - a false rejection that
    only ever fires on the freshest launches. See cfg.RPC_COMMITMENT.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [mint, {"encoding": "base64", "commitment": cfg.RPC_COMMITMENT}],
    }
    return await post_rpc(payload)


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

    Free payload checks first, then one RPC (authority), then the rug checks,
    then two Jupiter quotes. Cheapest rejection wins.
    """
    # --- Free: everything the listing payload already answers ---------------
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

    # Real buying pressure, straight from the payload. Unlike txns_h1 this is
    # always present on a fresh mint, and it predicts whether an exit exists.
    csol = curve_sol(meta)
    if cfg.MIN_CURVE_SOL and csol is not None and csol < cfg.MIN_CURVE_SOL:
        return (False, f"curve {csol:.3f} SOL < min {cfg.MIN_CURVE_SOL:.3f}")

    # DexScreener-derived momentum fields. Each is enforced ONLY when the source
    # actually reported it: for a token seconds old DexScreener has no pair yet,
    # and treating a missing field as 0 rejected 98.7% of all tokens scanned.
    liq = meta.get("liquidity_usd")
    if cfg.MIN_LIQUIDITY_USD and liq is not None and float(liq) < cfg.MIN_LIQUIDITY_USD:
        return (False, f"liquidity {float(liq):.0f} < min {cfg.MIN_LIQUIDITY_USD:.0f}")

    txns = meta.get("txns_h1")
    if txns is None:
        txns = meta.get("txns_24h")
    if cfg.MIN_TXNS_H1 and txns is not None and int(txns) < cfg.MIN_TXNS_H1:
        return (False, f"txns_h1 {int(txns)} < min {cfg.MIN_TXNS_H1}")

    pchg = meta.get("price_change_h1")
    if (cfg.MIN_PRICE_CHANGE_H1_PCT and pchg is not None
            and float(pchg) < cfg.MIN_PRICE_CHANGE_H1_PCT):
        return (False, f"price h1 {float(pchg):.1f}% < "
                       f"min {cfg.MIN_PRICE_CHANGE_H1_PCT:.1f}%")

    created = int(meta.get("pair_created_at") or 0)
    if cfg.MIN_PAIR_AGE_SEC and created:
        age = (int(time.time() * 1000) - created) / 1000.0
        if age < cfg.MIN_PAIR_AGE_SEC:
            return (False, f"pair too new ({age:.0f}s < {cfg.MIN_PAIR_AGE_SEC}s)")

    # --- One RPC call: mint/freeze authority renounced? ----------------------
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

    # --- Anti-rug: can one wallet dump the whole float? ---------------------
    if cfg.MAX_DEV_SHARE_PCT and mint:
        ok, why = await check_dev_share(meta)
        if not ok:
            return (False, why)

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


async def check_dev_share(meta: dict) -> tuple[bool, str]:
    """Reject a token whose creator still holds most of the sold float.

    This is the working substitute for check_holder_concentration: same threat
    model (one wallet able to dump the entire float in a single tx), but priced
    in one getTokenAccountBalance on the creator's derived ATA instead of
    getTokenLargestAccounts, which no reachable RPC endpoint serves.

    "Sold float" is what buyers have actually taken off the curve
    (CURVE_INITIAL_TOKENS - real_token_reserves), so the ratio answers the
    question that matters: of the tokens in circulation, how many can the dev
    dump? A creator with no token account at all holds nothing - that is a PASS,
    and it is the common case (7 of 15 sampled launches).

    Fails closed on RPC failure by default; see _unavailable().
    """
    mint, creator = meta.get("mint"), meta.get("creator")
    token_program = meta.get("token_program")
    if not (creator and token_program):
        return (True, "dev share unknown (payload has no creator)")

    sold = CURVE_INITIAL_TOKENS - float(meta.get("real_token_reserves") or 0)
    if sold <= 0:
        return (True, "OK")  # nothing in circulation yet - nothing to dump

    try:
        ata = derive_ata(creator, mint, token_program)
        body = await post_rpc({
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountBalance",
            "params": [ata, {"commitment": cfg.RPC_COMMITMENT}],
        })
    except Exception as e:
        return _unavailable("dev share check", e)

    if body.get("error"):
        # No such account => the creator never received (or already moved) any
        # tokens. Not an error condition for us.
        return (True, "OK")

    held = float((body.get("result") or {}).get("value", {}).get("amount") or 0)
    pct = held / sold * 100
    if pct > cfg.MAX_DEV_SHARE_PCT:
        return (False, f"dev holds {pct:.0f}% of float > "
                       f"max {cfg.MAX_DEV_SHARE_PCT:.0f}%")
    return (True, "OK")


def derive_ata(owner: str, mint: str, token_program: str) -> str:
    """Associated Token Account address for (owner, mint) under token_program."""
    from solders.pubkey import Pubkey

    addr, _ = Pubkey.find_program_address(
        [bytes(Pubkey.from_string(owner)),
         bytes(Pubkey.from_string(token_program)),
         bytes(Pubkey.from_string(mint))],
        Pubkey.from_string(ATA_PROGRAM),
    )
    return str(addr)


async def check_holder_concentration(mint: str) -> tuple[bool, str]:
    """Reject a token whose supply is concentrated in one non-curve wallet.

    A single wallet holding a large share of supply is the classic pump.fun rug
    setup: the dev (or a sniper bundle) can dump the whole float into the curve
    in one transaction. The bonding curve itself is excluded - it legitimately
    holds the unsold supply pre-graduation.

    One RPC call (getTokenLargestAccounts) - which every free endpoint currently
    refuses, so in practice this gate reports unavailable and check_dev_share is
    what protects the entry. Kept for when a paid RPC key is configured.
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts",
               "params": [mint, {"commitment": cfg.RPC_COMMITMENT}]}
    try:
        accounts = (await post_rpc(payload)).get("result", {}).get("value") or []
    except Exception as e:
        return _unavailable("holder check", e)

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
