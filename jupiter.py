"""
jupiter.py - Buy/Sell via Jupiter Ultra swap API.

SAFETY: every function is wrapped so that if cfg.LIVE_TRADING is False the
function returns a fake, clearly-labeled "paper" result and NEVER touches the
network for order submission. You must flip LIVE_TRADING=true in .env to trade
real funds.
"""
import httpx
from config import cfg
from wallet import load_keypair


async def _quote(input_mint: str, output_mint: str, amount_lamports: int) -> dict:
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount_lamports),
        "slippageBps": "50",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{cfg.JUPITER_ULTRA_URL}/order", params=params)
        r.raise_for_status()
        return r.json()


async def buy_token(mint: str, sol_amount: float) -> dict:
    """
    Buy `sol_amount` SOL worth of `mint`.
    Returns a result dict. In paper mode returns a fake result.
    """
    LAMPORTS = 1_000_000_000
    amount = int(sol_amount * LAMPORTS)
    WSOL = "So11111111111111111111111111111111111111112"

    if not cfg.LIVE_TRADING:
        return {
            "paper": True,
            "action": "BUY",
            "mint": mint,
            "sol_amount": sol_amount,
            "note": "DRY-RUN: no real transaction sent",
        }

    order = await _quote(WSOL, mint, amount)
    # Ultra returns a base64 transaction to sign with the wallet.
    kp = load_keypair()
    tx_b64 = order["transaction"]
    from solders.transaction import VersionedTransaction
    tx = VersionedTransaction.from_bytes(__import__("base64").b64decode(tx_b64))
    signed = kp.sign_versioned_transaction(tx)
    # Submit back to Ultra
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{cfg.JUPITER_ULTRA_URL}/order/{order['requestId']}/execute",
            json={"signedTransaction": __import__("base64").b64encode(bytes(signed)).decode()},
        )
        r.raise_for_status()
        return {"paper": False, "action": "BUY", "mint": mint, "result": r.json()}


async def sell_token(mint: str, token_amount: int) -> dict:
    """
    Sell `token_amount` base units of `mint` for SOL.
    """
    WSOL = "So11111111111111111111111111111111111111112"
    if not cfg.LIVE_TRADING:
        return {
            "paper": True,
            "action": "SELL",
            "mint": mint,
            "token_amount": token_amount,
            "note": "DRY-RUN: no real transaction sent",
        }
    order = await _quote(mint, WSOL, token_amount)
    kp = load_keypair()
    tx = VersionedTransaction.from_bytes(__import__("base64").b64decode(order["transaction"]))
    signed = kp.sign_versioned_transaction(tx)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{cfg.JUPITER_ULTRA_URL}/order/{order['requestId']}/execute",
            json={"signedTransaction": __import__("base64").b64encode(bytes(signed)).decode()},
        )
        r.raise_for_status()
        return {"paper": False, "action": "SELL", "mint": mint, "result": r.json()}
