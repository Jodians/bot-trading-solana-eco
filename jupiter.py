"""
jupiter.py - Buy/Sell + price quotes via Jupiter Ultra swap API.

SAFETY: every submit function is wrapped so that if cfg.LIVE_TRADING is False
it returns a fake, clearly-labeled "paper" result and NEVER touches the
network for order submission. You must flip LIVE_TRADING=true in .env to trade
real funds. Read-only quote helpers (get_sell_quote) are safe in any mode.
"""
import httpx
from config import cfg
from wallet import load_keypair

WSOL = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000


async def _quote(input_mint: str, output_mint: str, amount: int) -> dict:
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": "50",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{cfg.JUPITER_ULTRA_URL}/order", params=params)
        r.raise_for_status()
        return r.json()


def _out_amount_sol(order: dict) -> float:
    """Extract estimated output SOL from an Ultra order's embedded quote."""
    try:
        out = order.get("quote", {}).get("outAmount")
        if out is None:
            return 0.0
        return int(out) / LAMPORTS
    except Exception:
        return 0.0


async def get_sell_quote(mint: str, token_amount: int) -> float | None:
    """
    Read-only estimate: how much SOL we'd get selling `token_amount` base units
    of `mint`. Returns SOL float, or None on error. Never submits a transaction.
    """
    if token_amount <= 0:
        return 0.0
    try:
        order = await _quote(mint, WSOL, token_amount)
        return _out_amount_sol(order)
    except Exception as e:
        print(f"[quote] sell quote error: {e}")
        return None


async def buy_token(mint: str, sol_amount: float) -> dict:
    """
    Buy `sol_amount` SOL worth of `mint`.
    Returns a result dict including `token_amount` (base units) when live.
    In paper mode returns a simulated token_amount and no real transaction.
    """
    amount = int(sol_amount * LAMPORTS)
    if not cfg.LIVE_TRADING:
        # Simulate a plausible holding so paper TP/SL math has something to do.
        simulated_tokens = int(sol_amount * 1_000_000)  # 1M tokens per SOL (dummy)
        return {
            "paper": True,
            "action": "BUY",
            "mint": mint,
            "sol_amount": sol_amount,
            "token_amount": simulated_tokens,
            "note": "DRY-RUN: no real transaction sent",
        }

    order = await _quote(WSOL, mint, amount)
    kp = load_keypair()
    tx_b64 = order["transaction"]
    from solders.transaction import VersionedTransaction
    tx = VersionedTransaction.from_bytes(__import__("base64").b64decode(tx_b64))
    signed = kp.sign_versioned_transaction(tx)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{cfg.JUPITER_ULTRA_URL}/order/{order['requestId']}/execute",
            json={"signedTransaction": __import__("base64").b64encode(bytes(signed)).decode()},
        )
        r.raise_for_status()
        result = r.json()
    # Best-effort: derive token_amount from the order's quoted in/out if present.
    token_amount = 0
    try:
        token_amount = int(order.get("quote", {}).get("outAmount", 0))
    except Exception:
        token_amount = 0
    return {"paper": False, "action": "BUY", "mint": mint, "token_amount": token_amount, "result": result}


async def sell_token(mint: str, token_amount: int) -> dict:
    """
    Sell `token_amount` base units of `mint` for SOL.
    """
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
