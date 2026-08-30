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

    # pump.fun public listing endpoint (newest tokens)
    PUMPFUN_LISTING_URL = "https://frontend-api.pump.fun/coins?offset=0&limit=30&sort=created"

    # --- Advanced quality filters (reduce rug exposure) ---
    # Require a minimum on-chain liquidity (USD). Thin liquidity = easy rug.
    MIN_LIQUIDITY_USD = float(os.getenv("MIN_LIQUIDITY_USD", "0"))
    # Require a minimum txn count in the LAST HOUR (real interest / momentum).
    # Token baru pump.fun belum punya history 24h, jadi pakai h1.
    MIN_TXNS_H1 = int(os.getenv("MIN_TXNS_H1", "0"))
    # Require price to be UP over the last hour (momentum). 0 = no check.
    MIN_PRICE_CHANGE_H1_PCT = float(os.getenv("MIN_PRICE_CHANGE_H1_PCT", "0"))
    # Pair must be at least this many seconds old (skip brand-new, unproven pairs).
    MIN_PAIR_AGE_SEC = int(os.getenv("MIN_PAIR_AGE_SEC", "0"))

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
