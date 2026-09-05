"""Survey which leaderboard wallets can actually be studied from free RPC.

Discovery that forces this step: for the most-followed KOL wallets,
`getSignaturesForAddress` is flooded by FAILED transactions belonging to other
people's copy-trade bots, which merely reference the wallet inside an address
lookup table. Cented's last 1000 signatures were 100% failures spanning 0.3
minutes (~3,750 tx/min) with a foreign fee payer - so paginating to a useful
history window would cost thousands of calls and return almost no real trades.

One call per wallet tells us: success ratio, time span covered, and implied
tx/min. Wallets with a high ok% and a multi-hour span are tractable.
"""

import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
RPCS = [
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
    "https://solana.leorpc.com/?api_key=FREE",
]


def rpc(method, params, url, tries=4):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    delay = 0.8
    for _ in range(tries):
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json", "User-Agent": UA}
        )
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                d = json.loads(r.read())
            if "error" in d:
                time.sleep(delay)
                delay *= 1.8
                continue
            return d.get("result")
        except Exception:  # noqa: BLE001
            time.sleep(delay)
            delay *= 1.8
    return None


def survey(item):
    i, (name, wallet) = item
    url = RPCS[i % len(RPCS)]
    res = rpc("getSignaturesForAddress", [wallet, {"limit": 1000}], url)
    if not res:
        return {"name": name, "wallet": wallet, "error": "no_result"}
    ok = [x for x in res if not x.get("err")]
    ts = [x["blockTime"] for x in res if x.get("blockTime")]
    span_min = (max(ts) - min(ts)) / 60 if len(ts) > 1 else 0.0
    return {
        "name": name,
        "wallet": wallet,
        "n": len(res),
        "ok": len(ok),
        "ok_pct": round(100 * len(ok) / len(res), 1),
        "span_min": round(span_min, 1),
        "tx_per_min": round(len(res) / span_min, 1) if span_min else None,
        "newest_age_min": round((time.time() - max(ts)) / 60, 1) if ts else None,
    }


def main():
    src = sys.argv[1]
    rows = json.load(open(src, "r", encoding="utf-8"))
    # dedupe wallet -> best (largest) profit entry for a label
    best = {}
    for r in rows:
        w = r["wallet_address"]
        if w not in best or float(r.get("profit") or 0) > float(best[w].get("profit") or 0):
            best[w] = r
    wallets = [((r.get("name") or "?")[:18], w) for w, r in best.items()]
    print(f"unique wallets: {len(wallets)}", flush=True)

    out = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        for res in ex.map(survey, enumerate(wallets)):
            out.append(res)
            if "error" in res:
                print(f"  {res['name']:<18} ERROR", flush=True)
            else:
                print(f"  {res['name']:<18} ok={res['ok_pct']:>5}%  span={res['span_min']:>8} min"
                      f"  rate={res['tx_per_min']}  fresh={res['newest_age_min']}min ago", flush=True)

    good = [r for r in out if r.get("ok_pct", 0) >= 50 and r.get("span_min", 0) >= 120]
    good.sort(key=lambda r: -r["span_min"])
    print(f"\n=== TRACTABLE (ok>=50%, span>=2h): {len(good)}/{len(out)} ===")
    for r in good:
        print(f"  {r['name']:<18} ok={r['ok_pct']:>5}%  span={r['span_min']/60:>6.1f}h  "
              f"{r['wallet']}")

    dst = src.replace(".json", "_survey.json")
    json.dump(out, open(dst, "w", encoding="utf-8"), indent=1)
    print(f"\nwrote {dst}")


if __name__ == "__main__":
    main()
