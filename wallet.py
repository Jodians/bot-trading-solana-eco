"""
wallet.py - Load the bot's Solana keypair from the private key in .env, and
read its SOL balance.

Only used for LIVE trading. Paper mode never signs anything, and never needs a
balance (there are no fees to pay).
"""
from solders.keypair import Keypair
from solders.pubkey import Pubkey
import base58
from config import cfg
from rpc import post_rpc

LAMPORTS = 1_000_000_000


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


async def get_balance_sol() -> float | None:
    """
    SOL balance of the bot wallet, or None when it cannot be determined.

    None means "unknown", which callers must treat as a hard stop before a live
    buy: spending on an unverified balance is how a bot half-fills a swap and
    then cannot afford the sell. Paper mode never calls this.
    """
    try:
        pubkey = str(load_keypair().pubkey())
    except Exception as e:
        print(f"[wallet] cannot load keypair: {e}")
        return None
    try:
        body = await post_rpc({
            "jsonrpc": "2.0", "id": 1, "method": "getBalance",
            "params": [pubkey],
        }, timeout=15)
        if "error" in body:
            print(f"[wallet] getBalance error: {body['error']}")
            return None
        return int(body["result"]["value"]) / LAMPORTS
    except Exception as e:
        print(f"[wallet] getBalance failed: {e}")
        return None


if __name__ == "__main__":
    print("Bot wallet pubkey:", pubkey_str())
