"""
llm_analysis.py - Optional pre-buy quality gate using an LLM.

Provider: Conduit (OpenAI-compatible /chat/completions at CONDUIT_BASE_URL).
The LLM receives token metadata (name, symbol, socials, market cap, and any
fetched website/description text) and returns a JSON verdict:

    {"verdict": "BUY" | "PASS", "score": 0-100, "reason": "..."}

This module is NETWORK-ONLY when LLM_ANALYSIS_ENABLED is true. In paper mode it
still calls the API (it's just analysis, no chain action), but you can disable
it entirely with LLM_ANALYSIS_ENABLED=false to skip the call.

On any failure we FAIL-SAFE to "PASS" (do NOT buy) so a broken LLM step never
silently lets a bad token through.
"""
import json
import httpx
from config import cfg

_SYSTEM = (
    "You are a strict crypto token-quality screener for a Solana sniper bot. "
    "Given token metadata, judge whether it is likely a legitimate project worth "
    "sniping vs a scam/rug. Consider: presence of real socials, coherent branding, "
    "market cap sanity, obviously copycat/spam names, and any red flags. "
    "Respond ONLY with JSON: {\"verdict\":\"BUY\"|\"PASS\",\"score\":0-100,\"reason\":\"short\"}."
)


def _build_user_prompt(meta: dict, website_text: str = None) -> str:
    name = meta.get("name", "?")
    symbol = meta.get("symbol", "?")
    mcap = meta.get("usd_market_cap", 0)
    socials = []
    if meta.get("website"):
        socials.append(f"website={meta['website']}")
    if meta.get("twitter"):
        socials.append(f"twitter={meta['twitter']}")
    if meta.get("telegram"):
        socials.append(f"telegram={meta['telegram']}")
    socials_s = ", ".join(socials) if socials else "NONE"
    text = (
        f"Token: {name} ({symbol})\n"
        f"Market cap (USD): {mcap}\n"
        f"Socials: {socials_s}\n"
    )
    if website_text:
        text += f"Website/description snippet:\n{website_text[:1500]}\n"
    text += "Return JSON verdict now."
    return text


async def analyze_token(meta: dict, website_text: str = None) -> dict:
    """
    Returns {"verdict","score","reason"}. FAILS SAFE to PASS (no buy) on error.
    """
    if not cfg.LLM_ANALYSIS_ENABLED:
        return {"verdict": "BUY", "score": 100, "reason": "llm disabled - allow"}

    url = f"{cfg.CONDUIT_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg.CONDUIT_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": cfg.LLM_MODEL,
        "temperature": 0.2,
        "max_tokens": cfg.LLM_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _build_user_prompt(meta, website_text)},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, headers=headers, json=body)
            r.raise_for_status()
            data = r.json()
        content = data["choices"][0]["message"]["content"]
        # Strip code fences if present
        content = content.strip().strip("`").strip()
        if content.lower().startswith("json"):
            content = content[4:].strip()
        parsed = json.loads(content)
        verdict = str(parsed.get("verdict", "PASS")).upper()
        score = int(parsed.get("score", 0))
        reason = str(parsed.get("reason", ""))[:200]
        return {"verdict": verdict, "score": score, "reason": reason}
    except Exception as e:
        # Fail-safe: never buy on an error
        return {"verdict": "PASS", "score": 0, "reason": f"llm error: {e}"}


def passed(result: dict) -> bool:
    """True only if LLM explicitly says BUY and score >= threshold."""
    return result.get("verdict") == "BUY" and result.get("score", 0) >= cfg.LLM_MIN_SCORE
