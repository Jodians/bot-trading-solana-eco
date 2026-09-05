"""
config.py - Load bot configuration from .env
All trading is DISABLED by default (LIVE_TRADING=false).
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _bool(v: str, default=False) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y")


class Config:
    # RPC / API
    HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")
    HELIUS_RPC_URL = os.getenv(
        "HELIUS_RPC_URL",
        f"https://mainnet.helius-rpc.com/?api-key={os.getenv('HELIUS_API_KEY', '')}",
    )
    JUPITER_ULTRA_URL = os.getenv("JUPITER_ULTRA_URL", "https://lite-api.jup.ag/ultra/v1")

    # Comma-separated RPC endpoints tried in order when the primary returns 429
    # or 5xx. The Helius free tier exhausts its daily credit fast and then 429s
    # every getAccountInfo call, which made every token skip with "mint account
    # not found" / "authority check error" - i.e. discovery looked alive while
    # the gate was fully blind. Public endpoints have no key and are rate-limited
    # but sufficient for the read-only authority/holder checks.
    RPC_FALLBACK_URLS = [
        u.strip() for u in os.getenv(
            "RPC_FALLBACK_URLS",
            "https://api.mainnet-beta.solana.com,https://solana-rpc.publicnode.com",
        ).split(",") if u.strip()
    ]
    # Commitment level for the read-only account lookups behind the safety gates.
    #
    # This MUST NOT be left at the RPC default ("finalized"). Finalized lags the
    # chain by ~13s, so getAccountInfo on a mint that is seconds old returns
    # value=None - which filters.py reported as "mint account not found" and the
    # bot counted as a rejection. Measured against the 12 newest pump.fun coins:
    # every mint younger than ~15s was NOT-FOUND at finalized but FOUND at both
    # confirmed and processed. That is 31% of all skips in one run (2797/8957),
    # and it hit exactly the fresh launches a sniper exists to catch.
    #
    # "confirmed" is the right level: supermajority-voted, so the account data is
    # real, while still only ~1-2s behind. "processed" would be faster but can be
    # rolled back, and we do not want to gate a buy on data that may vanish.
    RPC_COMMITMENT = os.getenv("RPC_COMMITMENT", "confirmed")

    # Slippage tolerance for quotes AND live swaps, in basis points (100 = 1%).
    # 50 bps was hardcoded before; that is far too tight for a fresh pump.fun
    # token and would make most live swaps fail. Paper mode uses the same value
    # so quoted prices reflect what a live fill would actually tolerate.
    SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "1000"))

    # Wallet
    WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "")

    # Trading safety
    LIVE_TRADING = _bool(os.getenv("LIVE_TRADING", "false"), default=False)

    # Sizing
    BUY_AMOUNT_SOL = float(os.getenv("BUY_AMOUNT_SOL", "0.1"))

    # Exits
    TAKE_PROFIT_MULTIPLE = float(os.getenv("TAKE_PROFIT_MULTIPLE", "2.0"))
    STOP_LOSS_MULTIPLE = float(os.getenv("STOP_LOSS_MULTIPLE", "0.5"))
    MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
    # Sell delay after buy (seconds) - gives price time to move
    SELL_DELAY_SEC = int(os.getenv("SELL_DELAY_SEC", "30"))
    # How often (seconds) to re-check price for open positions (TP/SL monitor).
    PRICE_CHECK_SEC = int(os.getenv("PRICE_CHECK_SEC", "10"))

    # Filters
    REQUIRE_SOCIALS = _bool(os.getenv("REQUIRE_SOCIALS", "true"))
    REQUIRE_MINT_RENOUNCED = _bool(os.getenv("REQUIRE_MINT_RENOUNCED", "true"))
    REQUIRE_FREEZE_RENOUNCED = _bool(os.getenv("REQUIRE_FREEZE_RENOUNCED", "true"))
    ONLY_PRE_GRADUATION = _bool(os.getenv("ONLY_PRE_GRADUATION", "true"))
    MIN_MARKET_CAP_USD = float(os.getenv("MIN_MARKET_CAP_USD", "500"))
    MAX_MARKET_CAP_USD = float(os.getenv("MAX_MARKET_CAP_USD", "30000"))

    # --- LLM analysis (optional pre-buy quality gate) ---
    # Provider: Conduit (OpenAI-compatible chat/completions).
    # Set LLM_ANALYSIS_ENABLED=true to gate buys on an LLM verdict.
    # The model is configurable; default is a sane strong model on Conduit.
    LLM_ANALYSIS_ENABLED = _bool(os.getenv("LLM_ANALYSIS_ENABLED", "false"))
    CONDUIT_API_KEY = os.getenv("CONDUIT_API_KEY", "")
    CONDUIT_BASE_URL = os.getenv("CONDUIT_BASE_URL", "https://conduit.ozdoev.net/v1")
    LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")
    LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "400"))
    # Secondary provider (fallback kalau primary gagal/403/timeout). Kosongkan = gak pakai fallback.
    LLM_FALLBACK_BASE_URL = os.getenv("LLM_FALLBACK_BASE_URL", "")
    LLM_FALLBACK_API_KEY = os.getenv("LLM_FALLBACK_API_KEY", "")
    LLM_FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "")
    # Minimum score (0-100) for the LLM to allow a BUY.
    LLM_MIN_SCORE = int(os.getenv("LLM_MIN_SCORE", "60"))

    # --- Listener mode ---
    # Use Helius WebSocket (faster) instead of polling pump.fun listing.
    # Requires a valid HELIUS_API_KEY. Falls back to polling if false.
    USE_WEBSOCKET = _bool(os.getenv("USE_WEBSOCKET", "false"))

    # pump.fun public listing endpoint (newest tokens).
    # NOTE: the old frontend-api.pump.fun host is Cloudflare-blocked (HTTP 530)
    # for datacenter/bot traffic and never recovers. frontend-api-v3 is the
    # live host and needs sort=created_timestamp&order=DESC (plain
    # sort=created is not honoured there).
    PUMPFUN_LISTING_URL = os.getenv(
        "PUMPFUN_LISTING_URL",
        "https://frontend-api-v3.pump.fun/coins"
        "?offset=0&limit=30&sort=created_timestamp&order=DESC",
    )
    PUMPFUN_COIN_URL = os.getenv(
        "PUMPFUN_COIN_URL", "https://frontend-api-v3.pump.fun/coins"
    )

    # --- Advanced quality filters (reduce rug exposure) ---
    # Require a minimum on-chain liquidity (USD). Thin liquidity = easy rug.
    MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "0"))
    # Minimum SOL sitting in the bonding curve, read from the listing payload's
    # real_sol_reserves. This is the momentum/liquidity gate that actually works
    # on a fresh mint, because the value is always present.
    #
    # It replaces MIN_TXNS_H1 as the primary gate. txns_h1/liquidity_usd/
    # price_change_h1/pair_created_at come from DexScreener, and DexScreener has
    # no pair for a token seconds old: measured on 30 consecutive launches, all
    # four fields were absent 30/30 while usd_market_cap was present 30/30. The
    # old `int(meta.get("txns_h1") or 0)` therefore evaluated to 0 for every
    # token, so MIN_TXNS_H1=2 rejected 26928 of 27278 tokens (98.7%) in one run
    # for a reason that had nothing to do with the token.
    #
    # Curve SOL also predicts exitability, which txn count never did. Over 20
    # consecutive launches: below 0.1 SOL, 8 of 12 tokens had NO Jupiter sell
    # route at all; at or above 0.1 SOL, 8 of 8 were sellable.
    MIN_CURVE_SOL = float(os.getenv("MIN_CURVE_SOL", "0"))
    # Require a minimum txn count in the LAST HOUR (real interest / momentum).
    # Only enforced when the discovery source actually reports the field - an
    # absent field means "unknown", not "zero". See MIN_CURVE_SOL.
    MIN_TXNS_H1 = int(os.getenv("MIN_TXNS_H1", "0"))
    # Require price to be UP over the last hour (momentum). 0 = no check.
    MIN_PRICE_CHANGE_H1_PCT = float(os.getenv("MIN_PRICE_CHANGE_H1_PCT", "0"))
    # Pair must be at least this many seconds old (skip brand-new, unproven pairs).
    MIN_PAIR_AGE_SEC = int(os.getenv("MIN_PAIR_AGE_SEC", "0"))

    # --- Anti-rug gates -----------------------------------------------------
    # Max share of the circulating float (bonding curve excluded) a single
    # wallet may hold. A dev/bundle holding most of the float can dump it in one
    # tx. 0 disables the check (and saves one RPC call per token).
    #
    # NOTE: this gate needs getTokenLargestAccounts, which no reachable endpoint
    # currently serves (probed: mainnet-beta 429, publicnode 403, drpc 400,
    # omniatech 521, onfinality 429, blockeden 402, and the Helius key is spent).
    # It therefore fails open on essentially every token - the log fills with
    # "holder check unavailable" - so MAX_DEV_SHARE_PCT below is the gate that
    # actually bites. Keep this one configured for when a paid RPC key returns.
    MAX_TOP_HOLDER_PCT = float(os.getenv("MAX_TOP_HOLDER_PCT", "0"))
    # Max share of the SOLD float the creator's own wallet may hold. Same threat
    # as MAX_TOP_HOLDER_PCT (one wallet able to dump the whole float) but priced
    # in one getTokenAccountBalance on the creator's derived ATA - a call the
    # free endpoints do serve: 24 of 25 answered in a live probe at the bot's
    # real scan rate (3.9 calls/s), the miss being one transient HTTP error.
    #
    # Threshold from data, not taste: across 15 curve-eligible launches the
    # median dev share was 1.1% and 7 devs held nothing, while the rug shapes sat
    # at 50-324% of sold float. Anything in 20-50 rejects the same 6 tokens, so
    # 40 sits in the middle of that plateau. 0 disables.
    MAX_DEV_SHARE_PCT = float(os.getenv("MAX_DEV_SHARE_PCT", "0"))
    # What to do when a rug check cannot reach any RPC endpoint. false (default)
    # = treat unknown as unsafe and skip the token, matching the live-execution
    # rule that an unknown balance blocks a buy. Setting this true restores the
    # old fail-open behaviour, which silently disabled the anti-rug gates.
    RUG_CHECK_FAIL_OPEN = _bool(os.getenv("RUG_CHECK_FAIL_OPEN", "false"))
    # Minimum % of BUY_AMOUNT_SOL recoverable by immediately selling back.
    # Catches honeypots (no sell route at all) and liquidity too thin to exit.
    # Healthy pump.fun tokens sit around 88-95%. 0 disables (2 quotes per token).
    MIN_ROUND_TRIP_PCT = float(os.getenv("MIN_ROUND_TRIP_PCT", "0"))

    # --- Live-execution safety ----------------------------------------------
    # Hard cap on how long a position may be held, in seconds. Without this the
    # only exits are TP and SL, so a token that flatlines between them ties up
    # capital forever: a 45-minute paper run showed two positions pinned at
    # 0.97x with stdev 0.006 and 0.000 while 241 later candidates were rejected
    # for "max positions reached". 0 disables (not recommended for live).
    MAX_HOLD_SEC = int(os.getenv("MAX_HOLD_SEC", "0"))

    # SOL that must remain in the wallet after a buy, covering signature fees,
    # ATA rent and the eventual sell. Live buys are skipped when balance minus
    # BUY_AMOUNT_SOL would fall below this.
    MIN_SOL_RESERVE = float(os.getenv("MIN_SOL_RESERVE", "0.02"))

    # Extra priority fee (micro-lamports per compute unit) handed to Jupiter as
    # priorityFeeLamports. On pump.fun launches an unprioritised tx often never
    # lands. 0 = let Jupiter pick (it quotes ~500 lamports on a quiet market).
    PRIORITY_FEE_LAMPORTS = int(os.getenv("PRIORITY_FEE_LAMPORTS", "0"))

    # Seconds to wait for a live swap signature to confirm on-chain. Submitting
    # without confirming means the bot believes it holds tokens it may not.
    CONFIRM_TIMEOUT_SEC = int(os.getenv("CONFIRM_TIMEOUT_SEC", "45"))

    @classmethod
    def validate(cls):
        errors = []
        if cls.LIVE_TRADING:
            if not cls.WALLET_PRIVATE_KEY or cls.WALLET_PRIVATE_KEY.startswith("your_"):
                errors.append("LIVE_TRADING=true but WALLET_PRIVATE_KEY is not set.")
            else:
                # Reject obviously-invalid keys: a Solana secret seed decodes to
                # 32 (ed25519 seed) or 64 (full secret key) bytes from base58.
                try:
                    import base58
                    decoded = base58.b58decode(cls.WALLET_PRIVATE_KEY)
                    if len(decoded) not in (32, 64):
                        errors.append(
                            f"WALLET_PRIVATE_KEY decodes to {len(decoded)} bytes "
                            "(expected 32 or 64)."
                        )
                except Exception as e:
                    errors.append(f"WALLET_PRIVATE_KEY is not valid base58: {e}")
            if not cls.HELIUS_API_KEY or cls.HELIUS_API_KEY.startswith("your_"):
                errors.append("LIVE_TRADING=true but HELIUS_API_KEY is missing.")
        else:
            # Even in dry-run we warn if wallet key is a placeholder
            if cls.WALLET_PRIVATE_KEY.startswith("your_"):
                pass  # fine for paper mode

        # LLM analysis gate must have a key when enabled
        if cls.LLM_ANALYSIS_ENABLED:
            if not cls.CONDUIT_API_KEY or cls.CONDUIT_API_KEY.startswith("sk-cdt-your"):
                errors.append("LLM_ANALYSIS_ENABLED=true but CONDUIT_API_KEY is not set.")
            if not cls.LLM_MODEL:
                errors.append("LLM_ANALYSIS_ENABLED=true but LLM_MODEL is empty.")
        else:
            # Even in dry-run we warn if wallet key is a placeholder
            if cls.WALLET_PRIVATE_KEY.startswith("your_"):
                pass  # fine for paper mode
        if errors:
            raise ValueError("Config invalid:\n - " + "\n - ".join(errors))


# Convenience instance
cfg = Config()
