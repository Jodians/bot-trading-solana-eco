"""Study consistently-profitable Solana memecoin wallets from raw on-chain data.

Why raw RPC and not an explorer API: solscan.io is Cloudflare-blocked from this
host (403 on the site, 401 on pro-api without a paid token), Birdeye /
SolanaTracker / Vybe / Dune all require keys, and the project's own Helius key
is at "max usage reached". Free public RPC answers
getSignaturesForAddress + getTransaction, which is enough to rebuild PnL.

Pipeline
  1. wallets come from the kolscan.io leaderboard (1d/7d/30d) - the only free
     "who is actually profitable" list reachable here,
  2. getSignaturesForAddress, paginated,
  3. drop signatures with `err` set - a failed tx moves no state, so spending a
     getTransaction on it is pure waste (for some KOL wallets 97-100% of recent
     signatures are other people's failed copy-trade bots referencing the wallet
     through an address lookup table),
  4. getTransaction(encoding="json") concurrently across 3 endpoints, cached on
     disk by signature so re-runs are free,
  5. per tx compute the wallet's SOL delta (native + WSOL, fee added back when
     the wallet paid it) and token deltas from pre/postTokenBalances,
  6. FIFO lot matching per (wallet, mint) -> realized PnL, hold time, exit
     multiple, scale-in/scale-out counts.

Output is a behaviour profile used to calibrate this repo's sniper filters -
NOT a copy-trading tool.

Usage:
  python study.py '[["name","wallet"],...]' [max_sigs] [max_age_sec] [out.json]
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

LAMPORTS = 1_000_000_000
WSOL = "So11111111111111111111111111111111111111112"
SOL_LIKE = {WSOL}

# Endpoint pool. Helius (read from .env) goes first because it is the only one
# that reliably serves getTransaction, but its free tier is NOT a clean 10 RPS:
# measured on this host, 5 concurrent workers => 57/80 ok + 23x HTTP 429, and 10
# workers => only 23/80 ok. So concurrency is capped low and every 429 rotates
# onto the public endpoints instead of being retried against Helius alone.
# Also note: getHealth/getSlot do NOT consume credits, so an exhausted key still
# answers them "ok" - only a paying method like getSignaturesForAddress reveals
# a dead key.
def _helius_from_env() -> str | None:
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (
        os.path.join(here, os.pardir, ".env"),   # tools/ -> repo root
        os.path.join(here, ".env"),
    ):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    s = line.strip()
                    if s.startswith("HELIUS_API_KEY") and "=" in s:
                        key = s.split("=", 1)[1].strip()
                        if key and not key.startswith("your_"):
                            return f"https://mainnet.helius-rpc.com/?api-key={key}"
        except OSError:
            continue
    return None


RPCS = [u for u in (
    _helius_from_env(),
    "https://solana-rpc.publicnode.com",
    "https://api.mainnet-beta.solana.com",
    "https://solana.leorpc.com/?api_key=FREE",
) if u]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".wallet_cache")

_stats = Counter()
_slock = threading.Lock()


def _bump(k, n=1):
    with _slock:
        _stats[k] += n


def rpc(method: str, params, url: str | None = None, tries: int = 6):
    """One JSON-RPC call. `url` is only the PREFERRED first endpoint: on failure
    we rotate through the rest of RPCS, because a 429 from Helius is common and
    retrying the same exhausted endpoint just burns the retry budget."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    if url:
        start = RPCS.index(url) if url in RPCS else 0
        pool = [url] + [u for i, u in enumerate(RPCS) if i != start]
    else:
        pool = list(RPCS)
    delay = 0.7
    for attempt in range(tries):
        target = pool[attempt % len(pool)]
        req = urllib.request.Request(
            target, data=body, headers={"Content-Type": "application/json", "User-Agent": UA}
        )
        try:
            with urllib.request.urlopen(req, timeout=35) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            if "error" in d:
                code = d["error"].get("code")
                if code in (429, -32005, -32429):
                    _bump("rpc_429")
                    time.sleep(delay)
                    delay = min(delay * 1.9, 10)
                    continue
                _bump("rpc_error")
                return None
            _bump("rpc_ok")
            return d.get("result")
        except urllib.error.HTTPError as e:
            _bump(f"http_{e.code}")
            time.sleep(delay if e.code == 429 else 0.3)
            delay = min(delay * 1.9, 10)
        except Exception:  # noqa: BLE001
            _bump("rpc_exc")
            time.sleep(delay)
            delay = min(delay * 1.9, 10)
    return None


# --------------------------------------------------------------------------- #
def list_signatures(wallet: str, want: int, max_age_sec: int | None = None):
    out, before, now = [], None, time.time()
    while len(out) < want:
        opts = {"limit": min(1000, want - len(out))}
        if before:
            opts["before"] = before
        page = rpc("getSignaturesForAddress", [wallet, opts])
        if not page:
            break
        for rec in page:
            bt = rec.get("blockTime")
            if max_age_sec and bt and now - bt > max_age_sec:
                return out
            out.append(rec)
        before = page[-1]["signature"]
        if len(page) < opts["limit"]:
            break
    return out


def _cache_path(sig: str) -> str:
    sub = os.path.join(CACHE_DIR, sig[:2])
    os.makedirs(sub, exist_ok=True)
    return os.path.join(sub, sig + ".json")


def _slim(tx) -> dict:
    m = tx["meta"]
    return {
        "blockTime": tx.get("blockTime"),
        "fee": m.get("fee"),
        "preBalances": m.get("preBalances"),
        "postBalances": m.get("postBalances"),
        "preTokenBalances": m.get("preTokenBalances"),
        "postTokenBalances": m.get("postTokenBalances"),
        "accountKeys": tx["transaction"]["message"].get("accountKeys"),
        "loadedAddresses": m.get("loadedAddresses") or {},
    }


def fetch_one(args):
    sig, url = args
    p = _cache_path(sig)
    if os.path.exists(p):
        _bump("cache_hit")
        return sig
    tx = rpc("getTransaction", [sig, {"maxSupportedTransactionVersion": 0, "encoding": "json"}], url)
    if tx is None:
        _bump("fetch_failed")
        return None
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_slim(tx), fh)
    os.replace(tmp, p)
    return sig


def prefetch(sigs, workers: int = 4, progress_every: int = 200):
    """Download+cache every tx, then repair gaps sequentially.

    workers=4 is deliberate: Helius free tier starts 429ing at 5 concurrent on
    this host. Even at 4, ~10% of fetches die from connection churn (every
    endpoint answers 10/10 when probed sequentially, so it is concurrency, not
    the endpoints). A sequential repair pass over just the missing signatures
    costs little and takes the gap to ~0 - which matters because a missing SELL
    leaves a position wrongly counted as "still open" and silently inflates the
    win rate.
    """
    todo = [s["signature"] for s in sigs]
    jobs = [(s, RPCS[i % len(RPCS)]) for i, s in enumerate(todo)]
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for _ in ex.map(fetch_one, jobs):
            done += 1
            if progress_every and done % progress_every == 0:
                print(f"    fetched {done}/{len(jobs)}  (cache_hit={_stats['cache_hit']}, "
                      f"429={_stats['rpc_429']+_stats['http_429']}, "
                      f"fail={_stats['fetch_failed']})", flush=True)

    for attempt in (1, 2):
        missing = [s for s in todo if not os.path.exists(_cache_path(s))]
        if not missing:
            break
        print(f"    repair pass {attempt}: {len(missing)} missing, sequential", flush=True)
        for sig in missing:
            fetch_one((sig, RPCS[0]))
            time.sleep(0.12)
    still = sum(1 for s in todo if not os.path.exists(_cache_path(s)))
    if still:
        print(f"    WARNING: {still}/{len(todo)} tx unrecoverable - PnL is a lower bound",
              flush=True)
    return still


def load_tx(sig: str):
    p = _cache_path(sig)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def full_account_keys(tx) -> list:
    """v0 ordering: static keys, then ALT writable, then ALT readonly."""
    la = tx.get("loadedAddresses") or {}
    return list(tx.get("accountKeys") or []) + list(la.get("writable") or []) + list(
        la.get("readonly") or []
    )


def wallet_deltas(tx, wallet: str):
    """(sol_delta, {mint: qty_delta}) for `wallet`.

    Fee is added back when the wallet is the fee payer so an entry reads as the
    trade size. ATA rent (~0.002 SOL) is left in - known noise, immaterial next
    to entry sizes of 0.5-10 SOL.
    """
    keys = full_account_keys(tx)
    pre, post = tx.get("preBalances") or [], tx.get("postBalances") or []
    sol = 0.0
    if wallet in keys:
        idx = keys.index(wallet)
        if idx < len(pre) and idx < len(post):
            sol = (post[idx] - pre[idx]) / LAMPORTS
            if idx == 0 and tx.get("fee"):
                sol += tx["fee"] / LAMPORTS

    def by_mint(entries):
        agg = defaultdict(float)
        for e in entries or []:
            if e.get("owner") != wallet:
                continue
            amt = e.get("uiTokenAmount") or {}
            v = amt.get("uiAmount")
            if v is None:
                s = amt.get("uiAmountString")
                v = float(s) if s else 0.0
            agg[e["mint"]] += float(v or 0.0)
        return agg

    pre_t, post_t = by_mint(tx.get("preTokenBalances")), by_mint(tx.get("postTokenBalances"))
    toks = {}
    for m in set(pre_t) | set(post_t):
        d = post_t.get(m, 0.0) - pre_t.get(m, 0.0)
        if abs(d) < 1e-12:
            continue
        if m in SOL_LIKE:
            sol += d
        else:
            toks[m] = d
    return sol, toks


# --------------------------------------------------------------------------- #
class Position:
    __slots__ = ("mint", "lots", "buys", "sells", "cost_in", "sol_out", "first_buy",
                 "last_sell", "qty_bought", "qty_sold", "unpriced")

    def __init__(self, mint):
        self.mint = mint
        self.lots = deque()
        self.buys = self.sells = 0
        self.cost_in = self.sol_out = 0.0
        self.first_buy = self.last_sell = None
        self.qty_bought = self.qty_sold = 0.0
        self.unpriced = False


def _finish(pos: Position) -> dict:
    mult = (pos.sol_out / pos.cost_in) if pos.cost_in > 1e-9 else None
    hold = (pos.last_sell - pos.first_buy) if (pos.last_sell and pos.first_buy) else None
    return {
        "mint": pos.mint,
        "buys": pos.buys,
        "sells": pos.sells,
        "cost_sol": round(pos.cost_in, 6),
        "out_sol": round(pos.sol_out, 6),
        "pnl_sol": round(pos.sol_out - pos.cost_in, 6),
        "multiple": round(mult, 4) if mult else None,
        "hold_sec": hold,
        "first_buy": pos.first_buy,
        "unpriced": pos.unpriced,
        "still_open": False,
        "is_pumpfun": pos.mint.endswith("pump"),
    }


def reconstruct(wallet: str, sigs):
    open_pos: dict[str, Position] = {}
    closed: list[dict] = []
    kinds = Counter()
    ordered = sorted([s for s in sigs if not s.get("err")], key=lambda s: (s.get("blockTime") or 0))
    kinds["failed_skipped"] = len(sigs) - len(ordered)

    for rec in ordered:
        tx = load_tx(rec["signature"])
        if not tx:
            kinds["fetch_failed"] += 1
            continue
        ts = tx.get("blockTime") or rec.get("blockTime") or 0
        sol, toks = wallet_deltas(tx, wallet)
        if not toks:
            kinds["no_token_move"] += 1
            continue
        for mint, dq in toks.items():
            pos = open_pos.get(mint)
            if dq > 0:
                if pos is None:
                    pos = open_pos[mint] = Position(mint)
                    pos.first_buy = ts
                spend = -sol if sol < 0 else 0.0
                if spend <= 1e-9:
                    pos.unpriced = True
                    kinds["token_in_no_sol"] += 1
                else:
                    kinds["buy"] += 1
                pos.buys += 1
                pos.cost_in += spend
                pos.qty_bought += dq
                pos.lots.append([dq, (spend / dq) if dq else 0.0, ts])
            else:
                if pos is None:
                    kinds["sell_without_entry"] += 1
                    continue
                qty, proceeds = -dq, (sol if sol > 0 else 0.0)
                if proceeds <= 1e-9:
                    kinds["token_out_no_sol"] += 1
                else:
                    kinds["sell"] += 1
                pos.sells += 1
                pos.sol_out += proceeds
                pos.qty_sold += qty
                pos.last_sell = ts
                left = qty
                while left > 1e-12 and pos.lots:
                    lot = pos.lots[0]
                    take = min(lot[0], left)
                    lot[0] -= take
                    left -= take
                    if lot[0] <= 1e-12:
                        pos.lots.popleft()
                if sum(l[0] for l in pos.lots) <= max(1e-9, 0.01 * pos.qty_bought):
                    closed.append(_finish(pos))
                    del open_pos[mint]

    for pos in open_pos.values():
        d = _finish(pos)
        d["still_open"] = True
        closed.append(d)
    return closed, kinds


# --------------------------------------------------------------------------- #
def pct(vals, q):
    if not vals:
        return None
    s = sorted(vals)
    return s[max(0, min(len(s) - 1, int(round(q / 100 * (len(s) - 1)))))]


def summarize(name: str, wallet: str, trips: list, kinds: Counter) -> dict:
    good = [t for t in trips
            if t["multiple"] and not t["unpriced"] and not t["still_open"] and t["cost_sol"] > 0.01]
    wins = [t for t in good if t["multiple"] > 1]
    losses = [t for t in good if t["multiple"] <= 1]
    mults = [t["multiple"] for t in good]
    holds = [t["hold_sec"] for t in good if t["hold_sec"] is not None]
    costs = [t["cost_sol"] for t in good]
    pnl = sum(t["pnl_sol"] for t in good)

    fb = [t["first_buy"] for t in good if t["first_buy"]]
    span = (max(fb) - min(fb)) / 86400.0 if len(fb) > 1 else None

    b = Counter()
    for m in mults:
        b["<0.5x" if m < 0.5 else "0.5-0.8x" if m < 0.8 else "0.8-1.0x" if m < 1.0
          else "1.0-1.5x" if m < 1.5 else "1.5-2x" if m < 2 else "2-5x" if m < 5
          else "5-10x" if m < 10 else ">10x"] += 1

    s = {
        "name": name, "wallet": wallet,
        "round_trips": len(good), "open_positions": sum(1 for t in trips if t["still_open"]),
        "win_rate": round(len(wins) / len(good) * 100, 1) if good else None,
        "pnl_sol": round(pnl, 3),
        "entry_sol_median": pct(costs, 50), "entry_sol_p10": pct(costs, 10),
        "entry_sol_p90": pct(costs, 90), "entry_sol_max": max(costs) if costs else None,
        "hold_sec_p10": pct(holds, 10), "hold_sec_p25": pct(holds, 25),
        "hold_sec_median": pct(holds, 50), "hold_sec_p75": pct(holds, 75),
        "hold_sec_p90": pct(holds, 90),
        "mult_median": pct(mults, 50), "mult_p75": pct(mults, 75), "mult_p90": pct(mults, 90),
        "mult_max": max(mults) if mults else None,
        "win_mult_median": pct([t["multiple"] for t in wins], 50),
        "loss_mult_median": pct([t["multiple"] for t in losses], 50),
        "loss_mult_p10": pct([t["multiple"] for t in losses], 10),
        "sells_per_pos_median": pct([t["sells"] for t in good], 50),
        "buys_per_pos_median": pct([t["buys"] for t in good], 50),
        "scaleout_share": round(100 * sum(1 for t in good if t["sells"] > 1) / len(good), 1) if good else None,
        "scalein_share": round(100 * sum(1 for t in good if t["buys"] > 1) / len(good), 1) if good else None,
        "pumpfun_share": round(100 * sum(1 for t in good if t["is_pumpfun"]) / len(good), 1) if good else None,
        "trips_per_day": round(len(good) / span, 1) if span else None,
        "span_days": round(span, 2) if span else None,
        "mult_buckets": dict(b),
        "kinds": dict(kinds),
    }
    if good and pnl > 0:
        best = max(good, key=lambda t: t["pnl_sol"])
        top5 = sorted(good, key=lambda t: -t["pnl_sol"])[:5]
        s["best_trade_pnl"] = best["pnl_sol"]
        s["best_trade_mult"] = best["multiple"]
        s["best_share_of_pnl"] = round(best["pnl_sol"] / pnl * 100, 1)
        s["top5_share_of_pnl"] = round(sum(t["pnl_sol"] for t in top5) / pnl * 100, 1)
    s["trips"] = sorted(good, key=lambda t: -(t["pnl_sol"]))
    return s


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    wallets = json.loads(sys.argv[1])
    max_sigs = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    max_age = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    dst = sys.argv[4] if len(sys.argv) > 4 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "wallet_study.json")

    os.makedirs(CACHE_DIR, exist_ok=True)
    out = []
    for name, w in wallets:
        t0 = time.time()
        print(f"\n=== {name} {w} ===", flush=True)
        sigs = list_signatures(w, max_sigs, max_age or None)
        if not sigs:
            print("  no signatures", flush=True)
            continue
        failed = sum(1 for s in sigs if s.get("err"))
        ok = [s for s in sigs if not s.get("err")]
        ts = [s["blockTime"] for s in sigs if s.get("blockTime")]
        print(f"  signatures={len(sigs)} failed={failed} ({100*failed/len(sigs):.0f}% skipped) "
              f"span={(max(ts)-min(ts))/3600:.1f}h", flush=True)
        prefetch(ok)
        missing = sum(1 for s in ok if not os.path.exists(_cache_path(s["signature"])))
        trips, kinds = reconstruct(w, sigs)
        s = summarize(name, w, trips, kinds)
        s["fetch_sec"] = round(time.time() - t0, 1)
        s["tx_missing"] = missing
        s["tx_coverage_pct"] = round(100 * (len(ok) - missing) / len(ok), 1) if ok else None
        out.append(s)
        print(f"  trips={s['round_trips']} open={s['open_positions']} "
              f"win={s['win_rate']}% pnl={s['pnl_sol']} SOL "
              f"entry_med={s['entry_sol_median']} hold_med={s['hold_sec_median']}s "
              f"mult_med={s['mult_median']} coverage={s['tx_coverage_pct']}% "
              f"({s['fetch_sec']}s)", flush=True)

    with open(dst, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print(f"\nrpc stats: {dict(_stats)}")
    print(f"wrote {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
