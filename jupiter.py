"""
jupiter.py - Buy/Sell + price quotes via Jupiter Ultra swap API.

SAFETY: every submit function is wrapped so that if cfg.LIVE_TRADING is False
it returns a fake, clearly-labeled "paper" result and NEVER submits a
transaction. You must flip LIVE_TRADING=true in .env to trade real funds.
Read-only quote helpers (get_buy_quote / get_sell_quote) hit the network but
only ever ASK for a price - they are safe in any mode and are what paper mode
uses to mark positions to market.

Two bugs lived here and are worth remembering
--------------------------------------------
1. `outAmount` is a TOP-LEVEL field of an Ultra /order response. The old code
   read `order["quote"]["outAmount"]`, a subobject Ultra does not return, so
   EVERY quote silently resolved to 0. Downstream that meant paper positions
   priced at 0.0x (instant stop-loss) and live buys recording token_amount=0
   (position could never be sold). See _out_amount().
2. `sell_token` used to depend on a function-LOCAL `VersionedTransaction`
   import that only existed inside `buy_token`, so the first live sell raised
   NameError - buys worked, exits did not. The import is module-level now.
"""
import base64

import httpx
from solders.transaction import VersionedTransaction

from config import cfg
from wallet import load_keypair

WSOL = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000


async def _order(input_mint: str, output_mint: str, amount: int) -> dict:
    """Ask Ultra for an order (which doubles as a quote). Read-only."""
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(cfg.SLIPPAGE_BPS),
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{cfg.JUPITER_ULTRA_URL}/order", params=params)
        r.raise_for_status()
        return r.json()


def _out_amount(order: dict) -> int:
    """Output amount in base units, from the TOP level of the order object.

    Ultra shape: {"inAmount": "...", "outAmount": "...", "priceImpactPct": ...}
    There is no nested "quote" object - reading one is the bug described above.
    """
    raw = order.get("outAmount")
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


async def get_buy_quote(mint: str, sol_amount: float) -> int | None:
    """
    Read-only estimate: how many base units of `mint` would `sol_amount` SOL
    buy right now. Returns base units, or None when there is no route yet
    (brand-new mint, no pool) or the API errored. Never submits anything.
    """
    if sol_amount <= 0:
        return 0
    try:
        order = await _order(WSOL, mint, int(sol_amount * LAMPORTS))
        return _out_amount(order) or None
    except Exception as e:
        print(f"[quote] buy quote error for {mint}: {e}")
        return None


async def get_sell_quote(mint: str, token_amount: int) -> float | None:
    """
    Read-only estimate: how much SOL we'd get selling `token_amount` base units
    of `mint`. Returns SOL float, or None on error/no-route. Never submits.
    """
    if token_amount <= 0:
        return 0.0
    try:
        order = await _order(mint, WSOL, token_amount)
        return _out_amount(order) / LAMPORTS
    except Exception as e:
        print(f"[quote] sell quote error for {mint}: {e}")
        return None


async def _submit(order: dict) -> dict:
    """Sign an Ultra order and execute it. LIVE ONLY - callers must gate."""
    kp = load_keypair()
    tx = VersionedTransaction.from_bytes(base64.b64decode(order["transaction"]))
    signed = kp.sign_versioned_transaction(tx)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{cfg.JUPITER_ULTRA_URL}/order/{order['requestId']}/execute",
            json={"signedTransaction": base64.b64encode(bytes(signed)).decode()},
        )
        r.raise_for_status()
        return r.json()


async def buy_token(mint: str, sol_amount: float) -> dict:
    """
    Buy `sol_amount` SOL worth of `mint`.

    Returns a dict with `token_amount` (base units). In PAPER mode nothing is
    signed or sent, but the token_amount comes from a REAL Jupiter buy quote so
    the position can later be marked to market with a real sell quote. When no
    route exists yet the result carries quote_failed=True and the caller should
    decline to open a position rather than invent a holding.
    """
    if not cfg.LIVE_TRADING:
        tokens = await get_buy_quote(mint, sol_amount)
        if not tokens:
            return {
                "paper": True,
                "action": "BUY",
                "mint": mint,
                "sol_amount": sol_amount,
                "token_amount": 0,
                "quote_failed": True,
                "note": "DRY-RUN: no Jupiter route yet, no position opened",
            }
        return {
            "paper": True,
            "action": "BUY",
            "mint": mint,
            "sol_amount": sol_amount,
            "token_amount": tokens,
            "note": "DRY-RUN: no transaction sent; size from real Jupiter quote",
        }

    order = await _order(WSOL, mint, int(sol_amount * LAMPORTS))
    result = await _submit(order)
    return {
        "paper": False,
        "action": "BUY",
        "mint": mint,
        "token_amount": _out_amount(order),
        "result": result,
    }


async def sell_token(mint: str, token_amount: int) -> dict:
    """Sell `token_amount` base units of `mint` for SOL."""
    if not cfg.LIVE_TRADING:
        return {
            "paper": True,
            "action": "SELL",
            "mint": mint,
            "token_amount": token_amount,
            "note": "DRY-RUN: no real transaction sent",
        }
    order = await _order(mint, WSOL, token_amount)
    result = await _submit(order)
    return {
        "paper": False,
        "action": "SELL",
        "mint": mint,
        "sol_out": _out_amount(order) / LAMPORTS,
        "result": result,
    }
