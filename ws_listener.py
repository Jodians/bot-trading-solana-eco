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

# Dedup budgets for ws_listen (bounded so a long run cannot leak memory).
MAX_SEEN_MINTS = 5_000
MAX_SEEN_SIGS = 20_000
# Consecutive create-filter misses tolerated before assuming the log format changed.
CREATE_PROBE_N = 400


def _redact(url: str) -> str:
    """Strip the query string so an API key never lands in stdout/logs."""
    return url.split("?")[0] + ("?api-key=***" if "?" in url else "")


def _remember(store: set, key: str, cap: int):
    """Bounded membership set: drop an arbitrary half when the cap is hit."""
    store.add(key)
    if len(store) > cap:
        for _ in range(cap // 2):
            store.pop()


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


async def ws_listen(on_token, reconnect_delay: float = 3.0, seen: set = None):
    """
    Subscribe to pump.fun program logs via Helius WebSocket.
    on_token(meta_dict) is called for each discovered new token.

    Dedup: the logsSubscribe stream fires for EVERY transaction mentioning the
    pump.fun program (buys/sells too, not just token creation), and
    _extract_mint() resolves many of those to the same already-seen mint. Without
    a `seen` guard the same token is re-emitted dozens of times, spamming the
    dashboard feed and burning one getTransaction RPC call each time. We dedup on
    two levels:
      * signature - skip a tx we already handled (cheapest, before any RPC call)
      * mint      - skip a token already forwarded downstream
    Both sets are bounded so a long-running process cannot leak memory.
    """
    import json
    import websockets

    if seen is None:
        seen = set()
    seen_sigs = set()

    # Adaptive "create" filter. We only want token-creation transactions, but if
    # Helius/pump.fun ever change their log wording the filter would silently
    # starve discovery. So we watch it: after CREATE_PROBE_N consecutive misses
    # we assume the wording changed and fall back to mint-level dedup alone.
    create_filter = True
    probed = 0

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
                # Never log the raw URL: it carries the Helius API key as a query
                # param and stdout is captured into dashboard.log.
                print(f"[ws] subscribed to pump.fun logs ({_redact(ws_url)})")
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("method") != "logsNotification":
                        continue
                    params = msg["params"]["result"]
                    logs = params.get("value", {})
                    sig = logs.get("signature")
                    if not sig or sig in seen_sigs:
                        continue
                    _remember(seen_sigs, sig, MAX_SEEN_SIGS)
                    # Only "create" transactions mint a new token. Filtering on the
                    # log text avoids an RPC round-trip for ordinary trades.
                    log_lines = logs.get("logs") or []
                    if create_filter and log_lines:
                        if any("Instruction: Create" in ln for ln in log_lines):
                            probed = 0
                        else:
                            probed += 1
                            if probed >= CREATE_PROBE_N:
                                create_filter = False
                                print("[ws] 'Instruction: Create' never matched in "
                                      f"{CREATE_PROBE_N} notifications - disabling "
                                      "create-filter (log format may have changed); "
                                      "falling back to mint-level dedup only")
                            continue
                    tx = await _get_transaction(sig)
                    mint = _extract_mint(tx)
                    if not mint or mint in seen:
                        continue
                    _remember(seen, mint, MAX_SEEN_MINTS)
                    meta = await _fetch_token_meta(mint)
                    if meta:
                        await on_token(meta)
        except Exception as e:
            print(f"[ws] error: {e}; reconnecting in {reconnect_delay}s")
            await asyncio.sleep(reconnect_delay)
