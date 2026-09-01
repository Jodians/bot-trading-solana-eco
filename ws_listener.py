"""
ws_listener.py - Token discovery via Helius WebSocket (logsSubscribe).

Instead of polling pump.fun every 2s, we subscribe to logs mentioning the
pump.fun program and decode the Anchor `CreateEvent` straight out of the
`Program data:` log line (see pumpfun_events.py). The event carries the new
mint, name, symbol and metadata URI, so discovery needs NO extra RPC call and
no guessing.

History / why this shape
------------------------
The first version did logsSubscribe -> getTransaction(sig) -> heuristically
pick the new mint out of the account keys. That was one RPC round-trip per
notification and the heuristic ("scan preTokenBalances, else take the last
account key") silently resolved ordinary trades to already-seen mints, which is
what spammed the dashboard feed.

A text prefilter on "Instruction: Create" did not help either: pump.fun trades
routinely contain `Instruction: CreateTokenAccount` from OTHER programs, so the
substring matched non-creations, while genuine creations were not reliably
distinguishable from the log text alone. The event discriminator is exact.

getTransaction is kept ONLY as a fallback for the rare frame that looks like a
creation but fails to decode (program upgrade, truncated logs).

This module is NETWORK-ONLY (WebSocket + RPC). Paper mode is unaffected; it
only discovers tokens faster.
"""
import asyncio
import httpx

from config import cfg
from pumpfun_events import extract_new_mint, has_unknown_event

# pump.fun program id (constant on mainnet)
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

# Dedup budgets for ws_listen (bounded so a long run cannot leak memory).
MAX_SEEN_MINTS = 5_000
MAX_SEEN_SIGS = 20_000
# If this many notifications decode zero CreateEvents, the program's event layout
# probably changed -> warn once so discovery cannot starve silently.
CREATE_WARN_AFTER = 5_000


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
    """Fallback mint extraction from a fetched transaction.

    Only used when a frame carries pump.fun event data that we could not decode.
    Heuristic by nature - prefer the decoded CreateEvent whenever available.
    """
    if not tx:
        return None
    try:
        keys = tx["transaction"]["message"].get("accountKeys", [])
        pubkeys = [k if isinstance(k, str) else k.get("pubkey") for k in keys]
        for tb in tx.get("meta", {}).get("preTokenBalances", []):
            if tb.get("uiTokenAmount", {}).get("uiAmount", 0) == 0:
                return tb.get("mint")
        return pubkeys[-1] if pubkeys else None
    except Exception:
        return None


async def ws_listen(on_token, reconnect_delay: float = 3.0, seen: set = None):
    """
    Subscribe to pump.fun program logs via Helius WebSocket.
    on_token(meta_dict) is called once per newly created token.

    Dedup is two-level, both sets bounded:
      * signature - skip a frame we already handled (before any work)
      * mint      - skip a token already forwarded downstream
    """
    import json
    import websockets

    if seen is None:
        seen = set()
    seen_sigs = set()
    frames = creates = 0
    warned = False

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
                    value = msg["params"]["result"].get("value", {})
                    sig = value.get("signature")
                    if not sig or sig in seen_sigs:
                        continue
                    _remember(seen_sigs, sig, MAX_SEEN_SIGS)
                    if value.get("err"):  # failed tx never created anything
                        continue

                    logs = value.get("logs") or []
                    frames += 1
                    event = extract_new_mint(logs)

                    if event:
                        creates += 1
                        mint = event["mint"]
                    elif has_unknown_event(logs):
                        # An event tag we do not recognise: the program was likely
                        # upgraded. Pay for one RPC call rather than miss a launch.
                        # Recognised non-create events (trades, completions) fall
                        # through to `continue` and cost nothing.
                        mint = _extract_mint(await _get_transaction(sig))
                    else:
                        continue

                    if not mint or mint in seen:
                        continue
                    _remember(seen, mint, MAX_SEEN_MINTS)

                    if not warned and not creates and frames >= CREATE_WARN_AFTER:
                        warned = True
                        print(f"[ws] WARNING: {frames} notifications, zero CreateEvents "
                              "decoded - pump.fun's event layout may have changed "
                              "(see pumpfun_events.CREATE_EVENT)")

                    meta = await _fetch_token_meta(mint)
                    if meta:
                        # Trust the on-chain event for identity; the API/DexScreener
                        # fallback is often missing name/symbol for fresh mints.
                        if event:
                            meta.setdefault("mint", mint)
                            for k in ("name", "symbol"):
                                if event.get(k) and not meta.get(k):
                                    meta[k] = event[k]
                        await on_token(meta)
        except Exception as e:
            print(f"[ws] error: {e}; reconnecting in {reconnect_delay}s")
            await asyncio.sleep(reconnect_delay)
