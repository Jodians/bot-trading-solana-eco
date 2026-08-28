"""
ws_listener.py - Faster token discovery via Helius WebSocket (logsSubscribe).

Instead of polling pump.fun every 2s, we subscribe to logs mentioning the
pump.fun program. Each new token launch emits a "create" transaction; we grab
the signature, fetch the full transaction, and pull the new mint address from
its account keys. Metadata (name/symbol/socials/mcap) is then fetched the same
way the poller would, so the rest of the pipeline is unchanged.

Why signature->getTransaction instead of raw instruction decoding: it avoids
brittle manual decoding of pump.fun's binary layout and is robust to changes.
Cost: one extra RPC call per new token (fine; new tokens are not that frequent).

This module is NETWORK-ONLY (WebSocket + RPC). Paper mode is unaffected; it
only discovers tokens faster.
"""
import asyncio
import base64
import httpx
from config import cfg

# pump.fun program id (constant on mainnet)
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


async def _fetch_token_meta(mint: str) -> dict | None:
    """Pull lightweight metadata for a mint from the pump.fun API."""
    from pumpfun_listener import fetch_token_meta
    return await fetch_token_meta(mint)


async def _get_transaction(signature: str) -> dict | None:
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
        "params": [signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}],
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(cfg.HELIUS_RPC_URL, json=payload)
        r.raise_for_status()
        return r.json().get("result")


def _extract_mint(tx: dict) -> str | None:
    """New token mint is typically the first newly-created token account in the
    create instruction's account list. Heuristic: the mint is an account that
    did not exist before and is owned by the Token Program."""
    if not tx:
        return None
    try:
        msg = tx["transaction"]["message"]
        keys = msg.get("accountKeys", [])
        # accountKeys is a list of strings (json encoding) or [{pubkey,...}]
        pubkeys = [k if isinstance(k, str) else k.get("pubkey") for k in keys]
        # The mint is usually near the end of the create instruction's accounts.
        # Safer: scan for an account owned by Token Program (Tokenkeg...) that is
        # newly created. Helius marks accountIndex in meta.preTokenBalances.
        meta = tx.get("meta", {})
        ptb = meta.get("preTokenBalances", [])
        for tb in ptb:
            # mint with 0 pre-balance and owner = a non-program => likely new mint
            if tb.get("uiTokenAmount", {}).get("uiAmount", 0) == 0:
                return tb.get("mint")
        # Fallback: last account key (common for pump create)
        return pubkeys[-1] if pubkeys else None
    except Exception:
        return None


async def ws_listen(on_token, reconnect_delay: float = 3.0):
    """
    Subscribe to pump.fun program logs via Helius WebSocket.
    on_token(meta_dict) is called for each discovered new token.
    """
    import json
    import websockets

    ws_url = cfg.HELIUS_RPC_URL.replace("https://", "wss://").replace("http://", "ws://")
    subscribe = {
        "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
        "params": [
            {"mentions": [PUMP_PROGRAM]},
            {"commitment": "confirmed"},
        ],
    }
    while True:
        try:
            async with websockets.connect(ws_url) as ws:
                await ws.send(json.dumps(subscribe))
                print(f"[ws] subscribed to pump.fun logs ({ws_url})")
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("method") != "logsNotification":
                        continue
                    params = msg["params"]["result"]
                    logs = params.get("value", {})
                    sig = logs.get("signature")
                    if not sig:
                        continue
                    tx = await _get_transaction(sig)
                    mint = _extract_mint(tx)
                    if not mint:
                        continue
                    meta = await _fetch_token_meta(mint)
                    if meta:
                        await on_token(meta)
        except Exception as e:
            print(f"[ws] error: {e}; reconnecting in {reconnect_delay}s")
            await asyncio.sleep(reconnect_delay)
