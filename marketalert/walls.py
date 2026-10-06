"""OI snapshot, walls/floors, shift confirmation, OI change.

Snapshot format: {strike: {"CE": oi, "PE": oi}} (one expiry, or several summed per strike).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

Snapshot = dict[float, dict[str, float | None]]


@dataclass
class Level:
    strike: float
    oi: float


@dataclass
class Walls:
    spot: float
    wall: Level | None = None       # main wall: CE with highest OI at/above spot
    wall2: Level | None = None
    floor: Level | None = None      # main floor: PE with highest OI at/below spot
    floor2: Level | None = None
    pcr: float | None = None        # sum PE OI / sum CE OI over all fetched strikes
    ce_total: float = 0
    pe_total: float = 0
    excluded: list[float] = field(default_factory=list)


def sum_snapshots(snaps: list[Snapshot]) -> Snapshot:
    """Sum CE/PE OI per strike across expiries (missing values count as 0)."""
    out: Snapshot = {}
    for snap in snaps:
        for strike, sides in snap.items():
            row = out.setdefault(float(strike), {"CE": 0, "PE": 0})
            for side in ("CE", "PE"):
                row[side] = (row[side] or 0) + (sides.get(side) or 0)
    return out


def compute_walls(snap: Snapshot, spot: float) -> Walls:
    """Main/2nd wall and floor; the outermost fetched strikes can never be a wall or floor."""
    strikes = sorted(snap)
    excluded = [strikes[0], strikes[-1]] if strikes else []
    inner = [s for s in strikes if s not in excluded]

    def ranked(side: str, ok) -> list[Level]:
        cands = [Level(s, snap[s].get(side) or 0) for s in inner if ok(s)]
        cands = [c for c in cands if c.oi > 0]
        return sorted(cands, key=lambda c: (-c.oi, abs(c.strike - spot)))

    ce = ranked("CE", lambda s: s >= spot)
    pe = ranked("PE", lambda s: s <= spot)
    ce_total = sum(v.get("CE") or 0 for v in snap.values())
    pe_total = sum(v.get("PE") or 0 for v in snap.values())
    return Walls(spot=spot, wall=ce[0] if ce else None, wall2=ce[1] if len(ce) > 1 else None,
                 floor=pe[0] if pe else None, floor2=pe[1] if len(pe) > 1 else None,
                 pcr=round(pe_total / ce_total, 3) if ce_total else None,
                 ce_total=ce_total, pe_total=pe_total, excluded=excluded)


class WallTracker:
    """Confirms a main wall/floor strike change only after N consecutive polls."""

    SIDES = ("wall", "floor")

    def __init__(self, confirm_polls: int = 2):
        self.confirm = confirm_polls
        self.confirmed: dict[str, float | None] = {s: None for s in self.SIDES}
        self._cand: dict[str, float | None] = {s: None for s in self.SIDES}
        self._count: dict[str, int] = {s: 0 for s in self.SIDES}

    def update(self, walls: Walls) -> list[dict[str, Any]]:
        """Feed one poll; return confirmed shifts [{side, old, new}]."""
        shifts: list[dict[str, Any]] = []
        for side in self.SIDES:
            lvl = getattr(walls, side)
            if lvl is None:
                continue
            new = lvl.strike
            if self.confirmed[side] is None:
                self.confirmed[side] = new          # first observation: no shift event
            elif new == self.confirmed[side]:
                self._cand[side], self._count[side] = None, 0
            else:
                if new == self._cand[side]:
                    self._count[side] += 1
                else:
                    self._cand[side], self._count[side] = new, 1
                if self._count[side] >= self.confirm:
                    shifts.append({"side": side, "old": self.confirmed[side], "new": new})
                    self.confirmed[side] = new
                    self._cand[side], self._count[side] = None, 0
        return shifts


class OiHistory:
    """Snapshots of one trading day for OI-change % over a window (baseline >= 09:30)."""

    def __init__(self, window_minutes: int, not_before: time):
        self.window = timedelta(minutes=window_minutes)
        self.not_before = not_before
        self.snaps: list[tuple[datetime, Snapshot]] = []

    def add(self, ts: datetime, snap: Snapshot) -> None:
        if self.snaps and self.snaps[-1][0].date() != ts.date():
            self.snaps.clear()
        self.snaps.append((ts, snap))

    def baseline(self, now: datetime) -> tuple[datetime, Snapshot] | None:
        """Latest snapshot at least `window` old, same day, not before not_before."""
        best = None
        for ts, snap in self.snaps:
            if ts.date() == now.date() and ts.time() >= self.not_before and ts <= now - self.window:
                best = (ts, snap)
        return best

    def oi_change_pct(self, strike: float, side: str, now: datetime) -> float | None:
        """% change of OI at strike/side from the baseline to the latest snapshot <= now."""
        base = self.baseline(now)
        cur = next((s for ts, s in reversed(self.snaps) if ts <= now), None)
        if not base or cur is None:
            return None
        b = (base[1].get(strike) or {}).get(side) or 0
        c = (cur.get(strike) or {}).get(side)
        if not b or c is None:
            return None
        return round((c - b) / b * 100, 2)
