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

Concurrency shape (and why)
---------------------------
The reader coroutine does NOTHING but decode log frames and push mints onto a
queue; a pool of worker coroutines does the metadata enrichment. Earlier the
reader awaited an HTTP call inline, so for up to 15s per token it stopped
reading the socket, missed the server's keepalive ping and got disconnected -
538 `keepalive ping timeout` errors and 545 reconnects in one session log, each
costing a 3s blind gap. Keeping I/O off the read path is the fix.

A mint is only marked as seen once enrichment SUCCEEDS. The old order marked it
seen first, so a token whose metadata was not indexed yet (the normal case for
a mint seconds old: pump.fun API is Cloudflare-blocked and DexScreener has no
pair yet) was dropped permanently and never retried. That inverted the whole
point of a sniper - only tokens old enough to be indexed could ever be bought.
Failures now go back on the queue with a bounded number of delayed retries.

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

# Enrichment pipeline sizing.
ENRICH_WORKERS = 4
# Bounded so a burst cannot grow memory without limit. Full queue -> oldest
# pending mint is dropped (logged), which is preferable to stalling the reader.
QUEUE_MAX = 500
# A fresh mint is often not indexed anywhere yet. Retry a few times with a
# delay before giving up, instead of discarding it forever on the first miss.
ENRICH_MAX_ATTEMPTS = 4
ENRICH_RETRY_DELAY = 8.0
# On shutdown, how long to let already-decoded mints finish enrichment.
DRAIN_TIMEOUT = 30.0


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


def _merge_identity(meta: dict, event: dict | None, mint: str) -> dict:
    """Trust the on-chain CreateEvent for identity fields.

    The API/DexScreener fallback frequently has no name/symbol for a mint that
    is seconds old, while the event always carries them.
    """
    meta.setdefault("mint", mint)
    if event:
        for k in ("name", "symbol"):
            if event.get(k) and not meta.get(k):
                meta[k] = event[k]
    return meta


async def _enrich_worker(queue: asyncio.Queue, on_token, seen: set, inflight: set):
    """Consume (mint, event, attempt) jobs: fetch metadata, then hand downstream.

    Runs OFF the WebSocket read path so no HTTP latency can stall the socket.
    Marks a mint seen only after a successful enrichment, and requeues transient
    misses so a not-yet-indexed launch is not lost forever.

    A job with mint=None carries `_sig` instead: an undecodable pump.fun event
    that needs the getTransaction fallback to resolve a mint first.
    """
    while True:
        mint, event, attempt = await queue.get()
        done_with_mint = True
        try:
            # Fallback path: resolve the mint from the transaction (network call,
            # which is exactly why it belongs here and not in the reader).
            if mint is None:
                sig = (event or {}).get("_sig")
                if not sig:
                    continue
                try:
                    mint = _extract_mint(await _get_transaction(sig))
                except Exception as e:
                    print(f"[enrich] getTransaction failed for {sig[:16]}...: {e}")
                    continue
                event = None
                if not mint or mint in seen or mint in inflight:
                    continue
                inflight.add(mint)

            if mint in seen:
                continue

            meta = None
            try:
                meta = await _fetch_token_meta(mint)
            except Exception as e:
                print(f"[enrich] {mint}: fetch error: {e}")

            if meta:
                _remember(seen, mint, MAX_SEEN_MINTS)
                await on_token(_merge_identity(meta, event, mint))
                continue

            # No metadata yet (normal for a mint seconds old). Retry later
            # rather than discarding the launch. The mint stays in `inflight`
            # across retries so the reader will not enqueue it again.
            if attempt + 1 < ENRICH_MAX_ATTEMPTS:
                await asyncio.sleep(ENRICH_RETRY_DELAY)
                if _offer(queue, (mint, event, attempt + 1), inflight):
                    done_with_mint = False  # a successor job owns this mint now
            else:
                # Out of retries: mark seen so it stops consuming the pipeline.
                _remember(seen, mint, MAX_SEEN_MINTS)
                name = (event or {}).get("name") or mint[:8]
                print(f"[enrich] giving up on {name} ({mint}) after "
                      f"{ENRICH_MAX_ATTEMPTS} attempts - no metadata source")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[enrich] unexpected error for {mint}: {e}")
        finally:
            if mint is not None and done_with_mint:
                inflight.discard(mint)
            queue.task_done()


def _offer(queue: asyncio.Queue, job, inflight: set = None):
    """Non-blocking put so the reader never blocks (a stalled reader is what
    breaks the socket).

    When the queue is full, a decoded launch outranks an unresolved fallback job
    (mint=None): the fallback is dropped rather than evicting a real mint. Two
    real jobs fall back to dropping the oldest. Any mint we drop is released from
    `inflight`, otherwise the reader would never re-enqueue it.
    """
    def release(dropped_job):
        if inflight is not None and dropped_job and dropped_job[0]:
            inflight.discard(dropped_job[0])

    try:
        queue.put_nowait(job)
        return True
    except asyncio.QueueFull:
        pass

    if job[0] is None:  # low-priority fallback: never evict a real launch
        return False

    try:
        dropped = queue.get_nowait()
        queue.task_done()
        release(dropped)
        print(f"[ws] enrich queue full - dropped {dropped[0]}")
    except asyncio.QueueEmpty:
        pass
    try:
        queue.put_nowait(job)
        return True
    except asyncio.QueueFull:
        return False


async def ws_listen(on_token, reconnect_delay: float = 3.0, seen: set = None):
    """
    Subscribe to pump.fun program logs via Helius WebSocket.
    on_token(meta_dict) is called once per newly created token.

    The reader only decodes and enqueues; ENRICH_WORKERS coroutines do the
    network enrichment. Dedup is two-level, both sets bounded:
      * signature - skip a frame we already handled (before any work)
      * mint      - skip a token already forwarded downstream (set after a
                    SUCCESSFUL enrichment, so a not-yet-indexed mint is retried)
    """
    import json
    import websockets

    if seen is None:
        seen = set()
    seen_sigs = set()
    frames = creates = 0
    warned = False

    # Mints currently queued or being enriched (incl. across retries). Keeps the
    # reader from enqueueing the same mint twice before it is marked seen.
    inflight: set = set()
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
    workers = [
        asyncio.create_task(_enrich_worker(queue, on_token, seen, inflight))
        for _ in range(ENRICH_WORKERS)
    ]

    ws_url = cfg.HELIUS_RPC_URL.replace("https://", "wss://").replace("http://", "ws://")
    subscribe = {
        "jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
        "params": [
            {"mentions": [PUMP_PROGRAM]},
            {"commitment": "confirmed"},
        ],
    }
    try:
        while True:
            try:
                async with websockets.connect(ws_url) as ws:
                    await ws.send(json.dumps(subscribe))
                    # Never log the raw URL: it carries the Helius API key as a
                    # query param and stdout is captured into dashboard.log.
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
                            # An event tag we do not recognise: the program was
                            # likely upgraded. Queue an RPC-backed resolution
                            # rather than miss a launch - but do it off this
                            # loop, since getTransaction is a network call.
                            # Recognised non-create events (trades, completions)
                            # fall through to `continue` and cost nothing.
                            _offer(queue, (None, {"_sig": sig}, 0))
                            continue
                        else:
                            continue

                        if not mint or mint in seen or mint in inflight:
                            continue

                        if not warned and not creates and frames >= CREATE_WARN_AFTER:
                            warned = True
                            print(f"[ws] WARNING: {frames} notifications, zero "
                                  "CreateEvents decoded - pump.fun's event layout "
                                  "may have changed (see pumpfun_events.CREATE_EVENT)")

                        inflight.add(mint)
                        if not _offer(queue, (mint, event, 0), inflight):
                            inflight.discard(mint)
            except Exception as e:
                print(f"[ws] error: {e}; reconnecting in {reconnect_delay}s")
                await asyncio.sleep(reconnect_delay)
    finally:
        # Let queued enrichment finish before tearing the pool down, so a
        # shutdown (or a test's finite feed) does not silently discard mints
        # that were already decoded. Bounded so a stuck job cannot hang exit.
        try:
            await asyncio.wait_for(queue.join(), timeout=DRAIN_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        for w in workers:
            w.cancel()
