"""
telemetry.py - In-memory event bus + shared state for the live dashboard.

The bot (snipe.py) calls `tel.emit({...})` at key pipeline steps. The dashboard
WebSocket server subscribes to `tel` and pushes every event to connected
browsers. State (KPIs, token feed, open positions, P&L curve) is kept here so a
freshly connected client can request a full snapshot.

Safe to import even when the dashboard server is NOT running: emits simply
buffer into the ring buffers and broadcast to zero subscribers (no-op).
"""
import asyncio
import time
from collections import deque

MAX_FEED = 400
MAX_POS_HISTORY = 80
MAX_PNL_POINTS = 400


class Telemetry:
    def __init__(self):
        self.subscribers = set()  # websockets.WebSocketServerProtocol
        self.feed = deque(maxlen=MAX_FEED)  # token_new / eval / llm events
        self.positions = {}  # mint -> position state dict
        self.pnl_history = deque(maxlen=MAX_PNL_POINTS)  # [ts, equity] samples
        self.stats = {
            "scanned": 0,
            "passed": 0,
            "skipped": 0,
            "buys": 0,
            "exits_tp": 0,
            "exits_sl": 0,
            "realized_pnl_sol": 0.0,
            "start_ts": time.time(),
            "mode": "PAPER",
            "llm_enabled": False,
            # thresholds / config (filled at startup by run_dashboard)
            "th_tp": 2.0,
            "th_sl": 0.5,
            "buy_sol": 0.1,
            "max_pos": 3,
            "min_mcap": 500.0,
            "max_mcap": 30000.0,
        }
        self.paused = False

    # ---- equity / P&L curve ------------------------------------------------
    def current_equity(self) -> float:
        """Realized P&L + unrealized (live multiple) of open positions, in SOL."""
        realized = self.stats["realized_pnl_sol"]
        unreal = 0.0
        for p in self.positions.values():
            bs = p.get("buy_sol") or 0.0
            mult = p.get("multiple") or 0.0
            unreal += (mult - 1.0) * bs
        return realized + unreal

    def record_equity(self, ts=None):
        ts = ts or time.time()
        self.pnl_history.append([ts, round(self.current_equity(), 5)])
        return self.pnl_history[-1][1]

    async def push_pnl(self, ts=None):
        ts = ts or time.time()
        eq = self.record_equity(ts)
        realized = self.stats["realized_pnl_sol"]
        await self._broadcast({
            "type": "pnl",
            "ts": ts,
            "equity": round(eq, 5),
            "realized": round(realized, 5),
            "unrealized": round(eq - realized, 5),
        })

    # ---- event ingest ------------------------------------------------------
    async def emit(self, ev: dict):
        ev.setdefault("ts", time.time())
        kind = ev.get("type")

        if kind == "token_new":
            self.stats["scanned"] += 1
            self.feed.appendleft(ev)
        elif kind == "token_eval":
            if ev.get("passed"):
                self.stats["passed"] += 1
            else:
                self.stats["skipped"] += 1
        elif kind == "buy":
            self.stats["buys"] += 1
            self.positions[ev["mint"]] = {
                "mint": ev["mint"],
                "name": ev.get("name", "?"),
                "symbol": ev.get("symbol", ""),
                "buy_sol": ev.get("sol", 0.0),
                "paper": ev.get("paper", True),
                "bought_at": ev.get("ts", time.time()),
                "multiple": 0.0,
                "decision": "",
                "history": [],
            }
            await self.push_pnl(ev["ts"])
        elif kind == "position_tick":
            p = self.positions.get(ev["mint"])
            if p:
                p["multiple"] = ev.get("multiple", p["multiple"])
                p["decision"] = ev.get("decision", "")
                p["history"].append(p["multiple"])
                if len(p["history"]) > MAX_POS_HISTORY:
                    p["history"].pop(0)
            await self.push_pnl(ev.get("ts", time.time()))
        elif kind in ("exit_tp", "exit_sl"):
            key = "exits_tp" if kind == "exit_tp" else "exits_sl"
            self.stats[key] += 1
            mult = ev.get("multiple", 0.0)
            p = self.positions.pop(ev["mint"], None)
            if p and p.get("buy_sol"):
                self.stats["realized_pnl_sol"] += (mult - 1.0) * p["buy_sol"]
            await self.push_pnl(ev.get("ts", time.time()))

        await self._broadcast(ev)

    async def _broadcast(self, ev):
        if not self.subscribers:
            return
        dead = []
        msg = _dumps(ev)
        for ws in list(self.subscribers):
            try:
                await ws.send(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.subscribers.discard(ws)

    def snapshot(self):
        return {
            "type": "snapshot",
            "stats": dict(self.stats),
            "feed": list(self.feed),
            "positions": list(self.positions.values()),
            "pnl_history": [list(p) for p in self.pnl_history],
            "paused": self.paused,
        }

    async def set_pause(self, paused: bool):
        self.paused = paused
        await self.emit(
            {
                "type": "system",
                "message": f"Bot {'PAUSED' if paused else 'RESUMED'} from dashboard",
            }
        )


def _dumps(obj):
    import json

    return json.dumps(obj, default=str)


tel = Telemetry()
