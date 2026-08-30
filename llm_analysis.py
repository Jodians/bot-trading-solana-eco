"""
llm_analysis.py - Optional pre-buy quality gate using an LLM.

Primary provider: CONDUIT_BASE_URL / CONDUIT_API_KEY / LLM_MODEL
(di-set ke Justwoker lewat .env). Optional fallback: LLM_FALLBACK_* (misal Conduit).
Kalau primary gagal (403/timeout/error) -> coba fallback 1x -> kalau dua-duanya
gagal -> FAIL-SAFE ke PASS (jangan beli).

On any total failure we FAIL-SAFE to "PASS" (do NOT buy) so a broken LLM step
never silently lets a bad token through.
"""
import json
import httpx
from config import cfg

_SYSTEM = (
    "You are a strict crypto token-quality screener for a Solana sniper bot. "
    "Given token metadata, judge whether it is likely a legitimate project worth "
    "sniping vs a scam/rug. Consider: presence of real socials, coherent branding, "
    "market cap sanity, obviously copycat/spam names, and any red flags. "
    "Respond ONLY with JSON: {\"verdict\":\"BUY\"|\"PASS\",\"score\":0-100,\"reason\":\"short\"}. "
    "If you cannot verify legitimacy, return PASS with a low score."
)


def _extract_json(text: str) -> dict | None:
    if text is None:
        return None
    s = text.strip()
    # Drop a leading ```json / ``` fence
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
    start = s.find("{")
    if start == -1:
        return None
    end = s.rfind("}")
    if end <= start:
        return None
    candidate = s[start:end + 1]
    try:
        return json.loads(candidate)
    except Exception:
        return None


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
        "Judge whether this token is likely a legitimate project worth sniping vs a "
        "scam/rug. Consider real socials, coherent branding, market cap sanity, and "
        "red flags. Respond ONLY with a JSON object, no prose.\n\n"
        f"Token: {name} ({symbol})\n"
        f"Market cap (USD): {mcap}\n"
        f"Socials: {socials_s}\n"
    )
    if website_text:
        text += f"Website/description snippet:\n{website_text[:1500]}\n"
    text += 'Return exactly: {"verdict":"BUY"|"PASS","score":0-100,"reason":"short"}.'
    return text


async def _call_once(base_url: str, api_key: str, model: str, meta: dict, website_text: str) -> dict:
    """Satu call LLM. Raise kalau gagal (biar caller bisa fallback)."""
    if not api_key:
        raise ValueError("api_key kosong")
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": cfg.LLM_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _build_user_prompt(meta, website_text)},
        ],
        "response_format": {"type": "json_object"},
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()
    content = data["choices"][0]["message"]["content"]
    parsed = _extract_json(content)
    if parsed is None:
        raise ValueError(f"no JSON object in LLM output: {content[:120]}")
    verdict = str(parsed.get("verdict", "PASS")).upper()
    score = int(parsed.get("score", 0))
    reason = str(parsed.get("reason", ""))[:200]
    return {"verdict": verdict, "score": score, "reason": reason}


async def analyze_token(meta: dict, website_text: str = None) -> dict:
    """
    Returns {"verdict","score","reason"}. FAILS SAFE to PASS (no buy) on error.
    Coba primary -> retry 1x -> fallback provider (kalau di-set) -> fail-safe.
    """
    if not cfg.LLM_ANALYSIS_ENABLED:
        return {"verdict": "BUY", "score": 100, "reason": "llm disabled - allow"}

    providers = [("PRIMARY", cfg.CONDUIT_BASE_URL, cfg.CONDUIT_API_KEY, cfg.LLM_MODEL)]
    if cfg.LLM_FALLBACK_BASE_URL and cfg.LLM_FALLBACK_API_KEY:
        providers.append(("FALLBACK", cfg.LLM_FALLBACK_BASE_URL,
                          cfg.LLM_FALLBACK_API_KEY, cfg.LLM_FALLBACK_MODEL))

    errors = []
    for label, base, key, model in providers:
        # retry 1x per provider
        for attempt in range(2):
            try:
                res = await _call_once(base, key, model, meta, website_text)
                if label == "FALLBACK":
                    res["reason"] = f"[via fallback] {res['reason']}"
                return res
            except Exception as e:
                errors.append(f"{label} attempt{attempt+1}: {e}")
                continue

    # Dua-duanya gagal -> fail-safe (jangan beli)
    return {"verdict": "PASS", "score": 0, "reason": "llm error: " + " | ".join(errors)[:400]}


def passed(result: dict) -> bool:
    """True only if LLM explicitly says BUY and score >= threshold."""
    return result.get("verdict") == "BUY" and result.get("score", 0) >= cfg.LLM_MIN_SCORE
