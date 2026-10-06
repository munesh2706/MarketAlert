"""Engine core: per-index state fed by ticks and OI snapshots. Used live and in replay."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .candles import Candle, CandleSet
from .config import parse_hhmm
from .indicators import EmaSet
from .walls import OiHistory, Snapshot, WallTracker, Walls, compute_walls


class IndexState:
    """Candles, EMAs, wall tracker and OI history for one index."""

    def __init__(self, key: str, cfg: dict[str, Any]):
        self.key = key
        sch, oi, cnd = cfg["schedule"], cfg["oi"], cfg["candles"]
        self.ema_tf = cnd["ema_timeframe_minutes"]
        tfs = sorted({1, cnd["breakout_timeframe_minutes"], self.ema_tf})
        self.candles = CandleSet(tfs, parse_hhmm(sch["market_open"]), parse_hhmm(sch["market_close"]))
        self.emas = EmaSet(cfg["ema"]["periods"])
        self.tracker = WallTracker(oi["shift_confirm_polls"])
        self.oi_hist = OiHistory(oi["oi_change_window_minutes"], parse_hhmm(oi["oi_baseline_not_before"]))
        self.spot: float | None = None
        self.last_tick: datetime | None = None
        self.walls: Walls | None = None
        self.snapshot: Snapshot | None = None
        self.last_candle: dict[int, Candle] = {}

    def seed_emas(self, candles: list[tuple[datetime, float]]) -> bool:
        """Seed EMAs from historical 15-min (start, close) pairs; False if seeding failed."""
        return self.emas.seed(candles)

    def _on_candles(self, done: list[Candle]) -> list[dict[str, Any]]:
        events = []
        for c in done:
            self.last_candle[c.minutes] = c
            events.append({"type": "candle", "index": self.key, "candle": c})
            if c.minutes == self.ema_tf:
                self.emas.update(c.start, c.close)
                events.append({"type": "ema", "index": self.key, "ts": c.end,
                               "values": self.emas.values(), "close": c.close})
        return events

    def on_tick(self, ts: datetime, price: float) -> list[dict[str, Any]]:
        self.spot, self.last_tick = price, ts
        return self._on_candles(self.candles.on_tick(ts, price))

    def advance(self, now: datetime) -> list[dict[str, Any]]:
        return self._on_candles(self.candles.advance(now))

    def on_oi(self, ts: datetime, snap: Snapshot, spot: float | None = None) -> list[dict[str, Any]]:
        """Process an OI snapshot (summed across expiries as needed)."""
        spot = spot if spot is not None else self.spot
        if spot is None or not snap:
            return []
        if self.spot is None:
            self.spot = spot                  # REST spot until the first tick arrives
        self.snapshot = snap
        self.oi_hist.add(ts, snap)
        self.walls = compute_walls(snap, spot)
        events: list[dict[str, Any]] = [{"type": "walls", "index": self.key, "ts": ts, "walls": self.walls}]
        for s in self.tracker.update(self.walls):
            events.append({"type": "shift", "index": self.key, "ts": ts, **s})
        return events


class Engine:
    """All indices; thin dispatcher shared by live mode and replay."""

    def __init__(self, cfg: dict[str, Any], indices: list[str] | None = None):
        self.cfg = cfg
        self.states = {k: IndexState(k, cfg) for k in (indices or list(cfg["indices"]))}

    def on_tick(self, index: str, ts: datetime, price: float) -> list[dict[str, Any]]:
        return self.states[index].on_tick(ts, price)

    def on_oi(self, index: str, ts: datetime, snap: Snapshot, spot: float | None = None) -> list[dict[str, Any]]:
        return self.states[index].on_oi(ts, snap, spot)

    def advance(self, now: datetime) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for st in self.states.values():
            out += st.advance(now)
        return out


def fmt_level(lvl: Any) -> str:
    return f"{lvl.strike:g}({lvl.oi / 1e5:.1f}L)" if lvl else "-"


def timeline_line(st: IndexState, ts: datetime) -> str:
    """One timeline row: spot, walls/floors, PCR, EMA50/EMA200."""
    w = st.walls
    emas = " ".join(f"EMA{p}={v:.1f}" if v is not None else f"EMA{p}=None"
                    for p, v in st.emas.values().items())
    walls = (f"wall {fmt_level(w.wall)} / {fmt_level(w.wall2)}  floor {fmt_level(w.floor)} / "
             f"{fmt_level(w.floor2)}  PCR {w.pcr}") if w else "walls -"
    spot = f"{st.spot:.2f}" if st.spot is not None else "-"
    return f"{ts:%H:%M} {st.key:<9} spot {spot}  {walls}  {emas}"
