"""
jupiter.py - Buy/Sell + price quotes via Jupiter Ultra swap API.

SAFETY: every submit function is wrapped so that if cfg.LIVE_TRADING is False
it returns a fake, clearly-labeled "paper" result and NEVER submits a
transaction. You must flip LIVE_TRADING=true in .env to trade real funds.
Read-only quote helpers (get_buy_quote / get_sell_quote) hit the network but
only ever ASK for a price - they are safe in any mode and are what paper mode
uses to mark positions to market.

Three bugs lived here and are worth remembering
-----------------------------------------------
1. `outAmount` is a TOP-LEVEL field of an Ultra /order response. The old code
   read `order["quote"]["outAmount"]`, a subobject Ultra does not return, so
   EVERY quote silently resolved to 0. Downstream that meant paper positions
   priced at 0.0x (instant stop-loss) and live buys recording token_amount=0
   (position could never be sold). See _out_amount().
2. `sell_token` used to depend on a function-LOCAL `VersionedTransaction`
   import that only existed inside `buy_token`, so the first live sell raised
   NameError - buys worked, exits did not. The import is module-level now.
3. Ultra only builds a transaction when the request names a `taker`. Without it
   the response carries a price but NO `transaction` field, so the live path
   died on KeyError at the first real swap. Quotes deliberately stay
   taker-less (read-only, no wallet needed); only _order_for_swap() passes the
   taker and therefore gets something signable.
"""
import asyncio
import base64

import httpx
from solders.transaction import VersionedTransaction

from config import cfg
from rpc import post_rpc
from wallet import load_keypair

WSOL = "So11111111111111111111111111111111111111112"
LAMPORTS = 1_000_000_000


async def _order(input_mint: str, output_mint: str, amount: int,
                 taker: str | None = None) -> dict:
    """
    Ask Ultra for an order. Read-only unless `taker` is set.

    With no taker this is purely a price lookup. With a taker Ultra also returns
    an unsigned `transaction` plus `requestId`, which is what live swaps need.
    """
    params = {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": str(cfg.SLIPPAGE_BPS),
    }
    if taker:
        params["taker"] = taker
        # Ultra prices its own priority fee; override only when configured, as
        # an unprioritised tx frequently fails to land during a launch burst.
        if cfg.PRIORITY_FEE_LAMPORTS > 0:
            params["priorityFeeLamports"] = str(cfg.PRIORITY_FEE_LAMPORTS)
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


async def _confirm(signature: str) -> tuple[bool, str]:
    """
    Poll getSignatureStatuses until the swap lands or CONFIRM_TIMEOUT_SEC passes.

    Submitting and assuming success is how a bot ends up tracking a position it
    does not own (tx dropped) or missing one it does. Returns (ok, detail); an
    unconfirmed-but-not-failed tx returns ok=False so the caller does not record
    a fill it cannot prove.
    """
    if not signature:
        return False, "no signature returned"
    deadline = asyncio.get_event_loop().time() + max(cfg.CONFIRM_TIMEOUT_SEC, 1)
    last = "pending"
    while asyncio.get_event_loop().time() < deadline:
        try:
            body = await post_rpc({
                "jsonrpc": "2.0", "id": 1, "method": "getSignatureStatuses",
                "params": [[signature], {"searchTransactionHistory": True}],
            }, timeout=15)
            value = (body.get("result") or {}).get("value") or [None]
            st = value[0]
            if st:
                if st.get("err"):
                    return False, f"tx failed on-chain: {st['err']}"
                status = st.get("confirmationStatus") or "processed"
                last = status
                if status in ("confirmed", "finalized"):
                    return True, status
        except Exception as e:
            last = f"status check error: {e}"
        await asyncio.sleep(2)
    return False, f"not confirmed within {cfg.CONFIRM_TIMEOUT_SEC}s (last: {last})"


async def _submit(order: dict) -> dict:
    """
    Sign an Ultra order, execute it, and wait for on-chain confirmation.

    LIVE ONLY - callers must gate on cfg.LIVE_TRADING. The returned dict always
    carries `confirmed` so the caller can tell a landed swap from a submitted
    one, plus `signature` for manual inspection.
    """
    tx_b64 = order.get("transaction")
    if not tx_b64:
        # Happens when the order was fetched without a taker: price only.
        return {"confirmed": False, "error": "order has no transaction to sign"}
    kp = load_keypair()
    tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    signed = kp.sign_versioned_transaction(tx)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{cfg.JUPITER_ULTRA_URL}/order/{order['requestId']}/execute",
            json={"signedTransaction": base64.b64encode(bytes(signed)).decode()},
        )
        r.raise_for_status()
        result = r.json()

    signature = result.get("signature") or result.get("txid") or ""
    ok, detail = await _confirm(signature)
    print(f"[swap] signature={signature or '?'} confirmed={ok} ({detail})")
    return {"confirmed": ok, "signature": signature, "status": detail,
            "result": result}


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

    order = await _order(WSOL, mint, int(sol_amount * LAMPORTS),
                         taker=load_keypair().pubkey().__str__())
    res = await _submit(order)
    if not res.get("confirmed"):
        # No confirmed fill => no position. Recording one would have the monitor
        # trying to sell tokens the wallet may never have received.
        return {
            "paper": False,
            "action": "BUY",
            "mint": mint,
            "token_amount": 0,
            "quote_failed": True,
            "note": f"live buy not confirmed: {res.get('status') or res.get('error')}",
            "signature": res.get("signature", ""),
        }
    return {
        "paper": False,
        "action": "BUY",
        "mint": mint,
        "token_amount": _out_amount(order),
        "signature": res.get("signature", ""),
        "result": res,
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
    order = await _order(mint, WSOL, token_amount,
                         taker=load_keypair().pubkey().__str__())
    res = await _submit(order)
    out = {
        "paper": False,
        "action": "SELL",
        "mint": mint,
        "confirmed": bool(res.get("confirmed")),
        "signature": res.get("signature", ""),
        "result": res,
    }
    # Only claim proceeds for a confirmed sale; an unconfirmed one leaves the
    # position open on purpose so the monitor retries instead of booking P&L.
    if res.get("confirmed"):
        out["sol_out"] = _out_amount(order) / LAMPORTS
    else:
        out["note"] = f"live sell not confirmed: {res.get('status') or res.get('error')}"
    return out
