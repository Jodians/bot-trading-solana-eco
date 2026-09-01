"""
rpc.py - One shared JSON-RPC caller with automatic endpoint failover.

The Helius free tier burns through its credit quickly and then answers every
call with HTTP 429 "max usage reached". Before this module each caller talked
to cfg.HELIUS_RPC_URL directly, so an exhausted key turned every filter check
into "mint account not found" or "authority check error" - the bot kept
scanning but the safety gate was blind, which is strictly worse than stopping.

post_rpc() tries the primary endpoint first, then each entry of
cfg.RPC_FALLBACK_URLS, and remembers which endpoint last worked so a degraded
primary does not cost a wasted round trip on every single call.
"""
import asyncio

import httpx

from config import cfg

# Endpoint that answered most recently; tried first on the next call.
_preferred: str | None = None


def endpoints() -> list[str]:
    """Primary first, then fallbacks, with the last-known-good hoisted."""
    urls = [cfg.HELIUS_RPC_URL] + list(cfg.RPC_FALLBACK_URLS)
    seen, ordered = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            ordered.append(u)
    if _preferred and _preferred in ordered:
        ordered.remove(_preferred)
        ordered.insert(0, _preferred)
    return ordered


def _label(url: str) -> str:
    """Endpoint host for logs, with any api-key query string stripped."""
    return url.split("?")[0].replace("https://", "").replace("http://", "")


async def post_rpc(payload: dict, timeout: float = 10.0) -> dict:
    """
    POST a JSON-RPC payload, failing over on 429/5xx/transport errors.

    Returns the decoded JSON body. Raises the last exception only when every
    endpoint failed, so callers keep their existing error handling.
    """
    global _preferred
    last_exc: Exception | None = None

    for url in endpoints():
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(url, json=payload)
            if r.status_code == 429 or r.status_code >= 500:
                last_exc = httpx.HTTPStatusError(
                    f"{r.status_code} from {_label(url)}", request=r.request, response=r
                )
                continue
            r.raise_for_status()
            body = r.json()
            # A JSON-RPC level error (e.g. rate limit expressed in-band) should
            # also trigger failover rather than be handed back as a result.
            err = body.get("error")
            if isinstance(err, dict) and err.get("code") in (429, -32005, -32429):
                last_exc = RuntimeError(f"rpc error {err} from {_label(url)}")
                continue
            if url != _preferred:
                if _preferred is not None:
                    print(f"    [rpc] switched to {_label(url)}")
                _preferred = url
            return body
        except Exception as e:  # transport error, bad JSON, non-2xx
            last_exc = e
            continue

    raise last_exc if last_exc else RuntimeError("no RPC endpoint configured")
