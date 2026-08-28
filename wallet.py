"""
wallet.py - Load the bot's Solana keypair from the private key in .env.
Only used for LIVE trading. Paper mode never signs anything.
"""
from solders.keypair import Keypair
from solders.pubkey import Pubkey
import base58
from config import cfg


def load_keypair() -> Keypair:
    raw = cfg.WALLET_PRIVATE_KEY
    if not raw or raw.startswith("your_"):
        raise ValueError("WALLET_PRIVATE_KEY is not set (needed for live trading).")
    # base58-encoded seed (64-byte secret key)
    try:
        secret = base58.b58decode(raw)
        return Keypair.from_bytes(secret)
    except Exception as e:
        raise ValueError(f"Could not decode WALLET_PRIVATE_KEY as base58 seed: {e}")


def pubkey_str() -> str:
    try:
        return str(load_keypair().pubkey())
    except Exception:
        return "<no-wallet>"


if __name__ == "__main__":
    print("Bot wallet pubkey:", pubkey_str())
