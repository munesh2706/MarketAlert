"""Alert rules (AGENTS.md §8), arm/re-arm state, window, daily cap, mutes, message formatting.

Arm keys are "INDEX|TYPE|..." strings. A key present in state.arms with armed=False is
disarmed: the same (index, type, level) never alerts again until it is re-armed:
  ZONE            price >= reset_gap from the level, or the main level strike changes
  EMA             price >= reset_gap from the current EMA value
  BREAKOUT        price back >= reset_gap on the other side of the level
  OI_CHANGE       oi_change_rearm_minutes after firing
  BREAKOUT_FAILED one per breakout (unique key); WALL_SHIFT every confirmed change (no arm)
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import parse_hhmm
from .instruments import write_json_atomic

log = logging.getLogger("marketalert")

DAY_FIELDS = ("arms", "mutes", "counts", "last_walls", "map_walls", "pending", "types")


@dataclass
class Alert:
    index: str
    type: str
    key: str
    text: str
    ts: datetime


# ---------------------------------------------------------------- formatting
def fnum(x: float | None) -> str:
    """25008.4 -> '25,008'."""
    return "-" if x is None else f"{x:,.0f}"


def foi(x: float | None) -> str:
    """OI in lakhs / crores: 12400000 -> '1.24 Cr', 3320000 -> '33.2 L'."""
    if x is None:
        return "-"
    return f"{x / 1e7:.2f} Cr" if abs(x) >= 1e7 else f"{x / 1e5:.1f} L"


def fpts(d: float) -> str:
    return f"{abs(d):,.0f} pts"


def fpct(p: float) -> str:
    return f"{p:+.0f}%"


# ---------------------------------------------------------------- state
class AlertState:
    """Persistent alert state in data/state.json; day fields reset when the IST date changes."""

    def __init__(self, path: Path | None, today: date):
        self.path = path
        self.data: dict[str, Any] = {"tg_offset": 0}
        loaded: dict[str, Any] = {}
        if path and path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("state.json unreadable; starting fresh")
        self.data["tg_offset"] = loaded.get("tg_offset", 0)
        if loaded.get("date") == today.isoformat():
            self.data.update({k: loaded.get(k, self._empty(k)) for k in DAY_FIELDS}, date=today.isoformat())
        else:
            self.reset(today)

    @staticmethod
    def _empty(k: str) -> Any:
        return [] if k == "pending" else {}

    def reset(self, today: date) -> None:
        self.data.update({k: self._empty(k) for k in DAY_FIELDS}, date=today.isoformat())
        self.save()

    def roll(self, today: date) -> bool:
        """Reset day fields if the date changed. Returns True if reset."""
        if self.data.get("date") != today.isoformat():
            self.reset(today)
            return True
        return False

    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(self.path, self.data)

    def __getitem__(self, k: str) -> Any:
        return self.data[k]


# ---------------------------------------------------------------- engine
class AlertEngine:
    """Evaluates rules for one IndexState at a time. Caller serialises access (one lock)."""

    def __init__(self, cfg: dict[str, Any], state: AlertState):
        self.cfg = cfg
        self.state = state
        a = cfg["alerts"]
        self.start, self.end = parse_hhmm(a["window_start"]), parse_hhmm(a["window_end"])
        self.cap = a["max_alerts_per_index"]
        self._cap_logged: set[str] = set()

    # ---------------------------------------------------- window / mutes / cap
    def in_window(self, now: datetime) -> bool:
        return self.start <= now.time() < self.end

    def muted(self, index: str, now: datetime) -> bool:
        until = self.state["mutes"].get(index)
        return bool(until) and now < datetime.fromisoformat(until)

    def mute(self, index: str, minutes: float, now: datetime) -> list[str]:
        keys = list(self.cfg["indices"]) if index == "all" else [index]
        for k in keys:
            self.state["mutes"][k] = (now + timedelta(minutes=minutes)).isoformat()
        self.state.save()
        return keys

    def unmute(self, index: str) -> list[str]:
        keys = list(self.cfg["indices"]) if index == "all" else [index]
        for k in keys:
            self.state["mutes"].pop(k, None)
        self.state.save()
        return keys

    def count(self, index: str) -> int:
        return self.state["counts"].get(index, 0)

    def _armed(self, key: str) -> bool:
        e = self.state["arms"].get(key)
        return e is None or e.get("armed", True)

    def _emit(self, index: str, typ: str, key: str, text: str, now: datetime,
              ref: float | None = None, disarm: bool = True) -> list[Alert]:
        if not self.in_window(now) or self.muted(index, now):
            return []
        if self.count(index) >= self.cap:
            if index not in self._cap_logged:
                log.warning("%s daily alert cap %d reached", index, self.cap)
                self._cap_logged.add(index)
            return []
        if disarm:
            self.state["arms"][key] = {"armed": False, "ref": ref, "at": now.isoformat()}
        self.state["counts"][index] = self.count(index) + 1
        self.state["types"][typ] = self.state["types"].get(typ, 0) + 1
        self.state.save()
        return [Alert(index, typ, key, text, now)]

    # ---------------------------------------------------- entry point
    def process(self, st: Any, events: list[dict[str, Any]], now: datetime) -> list[Alert]:
        """Evaluate rules after engine events (tick/OI/advance) for one index."""
        out: list[Alert] = []
        bo_tf = self.cfg["candles"]["breakout_timeframe_minutes"]
        if st.spot is not None:
            self._rearm(st, now)                # before rules, so a due re-arm can fire now
        for ev in events:
            if ev["type"] == "candle" and ev["candle"].minutes == bo_tf:
                out += self._breakout(st, ev["candle"], now)
            elif ev["type"] == "walls":
                self._level_change(st)
                out += self._oi_change(st, now)
            elif ev["type"] == "shift":
                out += self._shift(st, ev, now)
        if st.spot is not None:
            out += self._zone(st, now)
            out += self._ema(st, now)
        return out

    # ---------------------------------------------------- re-arm
    def _rearm(self, st: Any, now: datetime) -> None:
        ix = self.cfg["indices"][st.key]
        gap, spot = ix["reset_gap"], st.spot
        emas = st.emas.values()
        changed = False
        for key, e in list(self.state["arms"].items()):
            parts = key.split("|")
            if parts[0] != st.key or e.get("armed", True):
                continue
            typ, ref = parts[1], e.get("ref")
            ok = False
            if typ == "ZONE":
                ok = abs(spot - ref) >= gap
            elif typ == "EMA":
                v = emas.get(int(parts[2]))
                ok = v is not None and abs(spot - v) >= gap
            elif typ == "BREAKOUT":
                ok = spot <= ref - gap if parts[2] == "up" else spot >= ref + gap
            elif typ == "OI_CHANGE":
                mins = self.cfg["alerts"]["oi_change_rearm_minutes"]
                ok = now - datetime.fromisoformat(e["at"]) >= timedelta(minutes=mins)
            if ok:
                del self.state["arms"][key]
                changed = True
        if changed:
            self.state.save()

    def _level_change(self, st: Any) -> None:
        """Main wall/floor strike changed -> re-arm that side's ZONE keys; remember last walls."""
        w = st.walls
        last = self.state["last_walls"].setdefault(st.key, {})
        cur = {"wall": w.wall.strike if w.wall else None, "floor": w.floor.strike if w.floor else None}
        for side, strike in cur.items():
            if last.get(side) is not None and strike != last.get(side):
                for key in [k for k in self.state["arms"] if k.startswith(f"{st.key}|ZONE|{side}|")]:
                    del self.state["arms"][key]
        if cur != {k: last.get(k) for k in cur}:
            last.update(cur, pcr=w.pcr)
            self.state.save()

    # ---------------------------------------------------- rules
    def _emas_near(self, st: Any, level: float, zone: float) -> list[str]:
        return [f"EMA{p} {fnum(v)} ⚡" for p, v in st.emas.values().items()
                if v is not None and abs(v - level) <= zone]

    def _zone(self, st: Any, now: datetime) -> list[Alert]:
        w = st.walls
        if not w:
            return []
        zone = self.cfg["indices"][st.key]["zone"]
        out: list[Alert] = []
        for side, lvl, lvl2, opt in (("wall", w.wall, w.wall2, "CE"), ("floor", w.floor, w.floor2, "PE")):
            if not lvl or abs(st.spot - lvl.strike) > zone:
                continue
            key = f"{st.key}|ZONE|{side}|{lvl.strike:g}"
            if not self._armed(key):
                continue
            pct = st.oi_hist.oi_change_pct(lvl.strike, opt, now)
            parts = [f"{'🔴' if side == 'wall' else '🟢'} {st.key} near {side.upper()} {fnum(lvl.strike)}",
                     f"Spot {fnum(st.spot)} ({fpts(st.spot - lvl.strike)})",
                     f"{opt} OI {foi(lvl.oi)}" + (f" ({fpct(pct)} 30m)" if pct is not None else "")]
            if lvl2:
                parts.append(f"2nd {side} {fnum(lvl2.strike)}")
            parts += self._emas_near(st, lvl.strike, zone)
            out += self._emit(st.key, "ZONE", key, " | ".join(parts), now, ref=lvl.strike)
        return out

    def _ema(self, st: Any, now: datetime) -> list[Alert]:
        zone = self.cfg["indices"][st.key]["zone"]
        w = st.walls
        out: list[Alert] = []
        for p, v in st.emas.values().items():
            if v is None or abs(st.spot - v) > zone:
                continue
            key = f"{st.key}|EMA|{p}"
            if not self._armed(key):
                continue
            above = st.spot >= v
            parts = [f"{'📈' if above else '📉'} {st.key} near 15m EMA{p} from {'above' if above else 'below'}",
                     f"Spot {fnum(st.spot)}", f"EMA{p} {fnum(v)} ({fpts(st.spot - v)})"]
            if w:
                parts.append(f"Wall {fnum(w.wall.strike if w.wall else None)} / "
                             f"Floor {fnum(w.floor.strike if w.floor else None)}")
                conf = [f"{name} {fnum(l.strike)}" for name, l in
                        (("Wall", w.wall), ("2nd wall", w.wall2), ("Floor", w.floor), ("2nd floor", w.floor2))
                        if l and abs(l.strike - v) <= zone]
                if conf:
                    parts.append("⚡ " + ", ".join(conf))
            out += self._emit(st.key, "EMA", key, " | ".join(parts), now, ref=v)
        return out

    def _breakout(self, st: Any, c: Any, now: datetime) -> list[Alert]:
        ix = self.cfg["indices"][st.key]
        out: list[Alert] = []
        n_fail = self.cfg["alerts"]["breakout_failed_candles"]
        # pending breakouts: was this candle's close back inside?
        keep, touched = [], False
        for pb in self.state["pending"]:
            if pb["index"] != st.key or datetime.fromisoformat(pb["candle"]) >= c.start:
                keep.append(pb)
                continue
            touched = True
            pb["count"] += 1
            lvl, up = pb["level"], pb["dir"] == "up"
            inside = c.close < lvl if up else c.close > lvl
            if inside:
                name = "WALL" if up else "FLOOR"
                text = (f"↩️ {st.key} BREAK{'OUT' if up else 'DOWN'} FAILED at {name} {fnum(lvl)} | "
                        f"5m close {fnum(c.close)} back inside ({fpts(c.close - lvl)} "
                        f"{'below' if up else 'above'}) | Spot {fnum(st.spot)}")
                out += self._emit(st.key, "BREAKOUT_FAILED",
                                  f"{st.key}|BREAKOUT_FAILED|{pb['dir']}|{lvl:g}|{pb['candle']}", text, now)
            elif pb["count"] < n_fail:
                keep.append(pb)
        if touched:
            self.state.data["pending"] = keep
            self.state.save()
        w = st.walls
        if not w:
            return out
        buf = ix["breakout_buffer"]
        for d, lvl, lvl2 in (("up", w.wall, w.wall2), ("down", w.floor, w.floor2)):
            if not lvl:
                continue
            beyond = c.close >= lvl.strike + buf if d == "up" else c.close <= lvl.strike - buf
            key = f"{st.key}|BREAKOUT|{d}|{lvl.strike:g}"
            if not beyond or not self._armed(key):
                continue
            side = "wall" if d == "up" else "floor"
            text = (f"{'🚀' if d == 'up' else '🔻'} {st.key} BREAK{'OUT above' if d == 'up' else 'DOWN below'} "
                    f"{side.upper()} {fnum(lvl.strike)} | 5m close {fnum(c.close)} "
                    f"({'+' if d == 'up' else '-'}{fpts(c.close - lvl.strike)}) | Spot {fnum(st.spot)}"
                    + (f" | 2nd {side} {fnum(lvl2.strike)}" if lvl2 else ""))
            got = self._emit(st.key, "BREAKOUT", key, text, now, ref=lvl.strike)
            if got:
                self.state["pending"].append({"index": st.key, "dir": d, "level": lvl.strike,
                                              "candle": c.start.isoformat(), "count": 0})
                self.state.save()
            out += got
        return out

    def _shift(self, st: Any, ev: dict[str, Any], now: datetime) -> list[Alert]:
        opt = "CE" if ev["side"] == "wall" else "PE"
        snap = st.snapshot or {}
        old_oi = (snap.get(ev["old"]) or {}).get(opt)
        new_oi = (snap.get(ev["new"]) or {}).get(opt)
        text = (f"🔀 {st.key} {ev['side'].upper()} SHIFT {fnum(ev['old'])} → {fnum(ev['new'])} | "
                f"{opt} OI {foi(old_oi)} → {foi(new_oi)} | Spot {fnum(st.spot)} "
                f"({fpts(st.spot - ev['new'])} to new {ev['side']})")
        key = f"{st.key}|WALL_SHIFT|{ev['side']}|{ev['old']:g}>{ev['new']:g}|{now.isoformat()}"
        return self._emit(st.key, "WALL_SHIFT", key, text, now, disarm=False)

    def _oi_change(self, st: Any, now: datetime) -> list[Alert]:
        w = st.walls
        thr = self.cfg["oi"]["oi_change_pct"]
        out: list[Alert] = []
        for side, lvl, opt in (("wall", w.wall, "CE"), ("floor", w.floor, "PE")):
            if not lvl:
                continue
            pct = st.oi_hist.oi_change_pct(lvl.strike, opt, now)
            key = f"{st.key}|OI_CHANGE|{side}|{lvl.strike:g}"
            if pct is None or abs(pct) < thr or not self._armed(key):
                continue
            meaning = "writers adding" if pct > 0 else "writers leaving"
            text = (f"📊 {st.key} {side.upper()} {fnum(lvl.strike)} {opt} OI {fpct(pct)} in 30m ({meaning}) | "
                    f"OI {foi(lvl.oi)} | Spot {fnum(st.spot)} ({fpts(st.spot - lvl.strike)})")
            out += self._emit(st.key, "OI_CHANGE", key, text, now, ref=lvl.strike)
        return out


# ---------------------------------------------------------------- map / summary
def index_block(st: Any, info: dict[str, Any] | None = None) -> str:
    """Market-map block for one index (starts with the index name)."""
    info = info or {}
    w = st.walls
    emas = " | ".join(f"EMA{p} {fnum(v)}" for p, v in st.emas.values().items())
    exp = info.get("expiry")
    lines = [f"{st.key} {fnum(st.spot)}" + (f" | Exp {exp:%d-%b}" if exp else "")
             + (f" ({info['note']})" if info.get("note") else "")]
    if w:
        lines.append(f"Walls {fnum(w.wall.strike if w.wall else None)} ({foi(w.wall.oi if w.wall else None)}) / "
                     f"{fnum(w.wall2.strike if w.wall2 else None)} ({foi(w.wall2.oi if w.wall2 else None)})")
        lines.append(f"Floors {fnum(w.floor.strike if w.floor else None)} ({foi(w.floor.oi if w.floor else None)}) / "
                     f"{fnum(w.floor2.strike if w.floor2 else None)} ({foi(w.floor2.oi if w.floor2 else None)})")
        lines.append(f"PCR {w.pcr} | {emas}")
    else:
        lines.append(f"Walls - (no OI yet) | {emas}")
    return "\n".join(lines)


def build_map(states: dict[str, Any], infos: dict[str, dict[str, Any]], now: datetime,
              keys: list[str] | None = None) -> str:
    keys = keys or list(states)
    blocks = [index_block(states[k], infos.get(k)) for k in keys]
    if len(keys) == 1:
        return blocks[0]
    return f"🗺 Market map {now:%H:%M}\n\n" + "\n\n".join(blocks)


def build_summary(states: dict[str, Any], alerts: AlertEngine, now: datetime) -> str:
    st_data = alerts.state
    top = alerts.cfg["alerts"]["summary_top_oi_changes"]
    blocks = []
    for k, st in states.items():
        o = st.ohlc or {}
        start = st_data["map_walls"].get(k, {})
        w = st.walls
        lines = [f"{k} O {fnum(o.get('open'))} H {fnum(o.get('high'))} L {fnum(o.get('low'))} "
                 f"C {fnum(o.get('close'))}"]
        if w:
            lines.append(f"Wall {fnum(start.get('wall'))} → {fnum(w.wall.strike if w.wall else None)} | "
                         f"Floor {fnum(start.get('floor'))} → {fnum(w.floor.strike if w.floor else None)}")
            ch = st.oi_hist.day_changes(top)
            if ch:
                lines.append("OI Δ: " + ", ".join(
                    f"{fnum(r['strike'])} {r['side']} {'+' if r['change'] >= 0 else '-'}{foi(abs(r['change']))}"
                    + (f" ({fpct(r['pct'])})" if r["pct"] is not None else "") for r in ch))
            lines.append(f"PCR {start.get('pcr', '-')} → {w.pcr} | Alerts {alerts.count(k)}")
        else:
            lines.append(f"No OI data | Alerts {alerts.count(k)}")
        blocks.append("\n".join(lines))
    return f"📋 Summary {now:%d-%b}\n\n" + "\n\n".join(blocks)


def record_map_walls(states: dict[str, Any], alerts: AlertEngine) -> None:
    """Remember walls/floors/PCR at map time for the summary (persisted)."""
    for k, st in states.items():
        w = st.walls
        if w:
            alerts.state["map_walls"][k] = {"wall": w.wall.strike if w.wall else None,
                                            "floor": w.floor.strike if w.floor else None, "pcr": w.pcr}
    alerts.state.save()
