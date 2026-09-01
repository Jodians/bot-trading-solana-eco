"""
positions_store.py - crash-safe persistence for open positions.

Why this exists
---------------
Positions used to live only in a process-local dict. In paper mode that is
harmless. In LIVE mode it is the most dangerous gap in the bot: the launcher
(`Solana Dashboard.bat`) auto-restarts on exit, so any restart left real tokens
held on-chain with NO monitor attached - meaning take-profit and stop-loss would
never fire again. The position simply sat there until sold by hand.

Writes are atomic (write temp in the same directory, then os.replace) so a crash
mid-write cannot leave a truncated file that loses every position at once. A
partially-written file is worse than no file: it would silently drop holdings.
"""
import json
import os
import tempfile

# Kept next to the code by default so a restart from any cwd finds it.
PATH = os.getenv("POSITIONS_FILE") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "positions.json"
)

# Only these keys are persisted. `meta` can carry anything the listener/API
# returned; storing it whole risks non-serializable junk, and the monitor only
# needs the display name.
_KEYS = ("bought_at", "buy_sol", "token_amount", "paper", "name")


def _row(mint: str, pos: dict) -> dict:
    row = {k: pos.get(k) for k in _KEYS if k in pos}
    row.setdefault("name", (pos.get("meta") or {}).get("name", mint))
    return row


def save(positions: dict) -> None:
    """Atomically persist the open-position map. Never raises."""
    try:
        payload = {m: _row(m, p) for m, p in positions.items()}
        d = os.path.dirname(PATH) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".positions-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            os.replace(tmp, PATH)
        except Exception:
            os.path.exists(tmp) and os.unlink(tmp)
            raise
    except Exception as e:
        # Persistence must never take the bot down mid-trade.
        print(f"[store] could not save positions: {e}")


def load() -> dict:
    """
    Return persisted positions as {mint: pos}. Empty dict when absent/corrupt.

    Rows without a positive token_amount are dropped: they cannot be sold, so
    re-attaching a monitor to them would only spin. `meta` is rebuilt from the
    stored name so callers can keep reading pos["meta"]["name"].
    """
    try:
        with open(PATH, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[store] ignoring unreadable {PATH}: {e}")
        return {}

    out = {}
    for mint, row in raw.items():
        if not isinstance(row, dict):
            continue
        try:
            tokens = int(row.get("token_amount") or 0)
            buy_sol = float(row.get("buy_sol") or 0)
        except (TypeError, ValueError):
            continue
        if tokens <= 0 or buy_sol <= 0:
            print(f"[store] dropping unsellable persisted row {mint}")
            continue
        name = row.get("name") or mint
        out[mint] = {
            "bought_at": float(row.get("bought_at") or 0),
            "buy_sol": buy_sol,
            "token_amount": tokens,
            "paper": bool(row.get("paper", True)),
            "meta": {"name": name, "mint": mint},
            "restored": True,
        }
    return out
