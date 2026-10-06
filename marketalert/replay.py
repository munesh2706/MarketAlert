"""Run the engine on a recorded day with a simulated clock and print a timeline.

Timeline: wall/floor values, PCR and EMA50/EMA200 at every completed N-minute candle
(replay.timeline_minutes), plus confirmed wall/floor shifts as they happen.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .alerts import AlertEngine, AlertState
from .config import IST
from .engine import Engine, timeline_line


def _ts(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, IST)


def load_day(day_dir: Path, indices: list[str]) -> tuple[list[tuple], dict[str, list]]:
    """Return (events sorted by time, seeds). Event = (ts, order, kind, index, payload)."""
    events: list[tuple] = []
    seeds: dict[str, list] = {}
    for idx in indices:
        tf = day_dir / f"{idx}_ticks.jsonl"
        if tf.exists():
            for line in tf.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    t, p = json.loads(line)[:2]
                    events.append((_ts(t), 0, "tick", idx, p))
        of = day_dir / f"{idx}_oi.jsonl"
        if of.exists():
            for line in of.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    snap = {float(k): {"CE": v[0], "PE": v[1]} for k, v in r["oi"].items()}
                    events.append((_ts(r["t"]), 1, "oi", idx, (r.get("spot"), snap)))
        sf = day_dir / f"{idx}_seed.json"
        if sf.exists():
            seeds[idx] = [(datetime.fromisoformat(t), c) for t, c in json.loads(sf.read_text(encoding="utf-8"))]
    events.sort(key=lambda e: (e[0], e[1]))
    return events, seeds


def replay_day(cfg: dict[str, Any], day_dir: Path, indices: list[str] | None = None,
               out: Callable[[str], None] = print, alerts: bool = True,
               on_alert: Callable[[Any], None] | None = None) -> dict[str, Any]:
    """Feed a recorded day through the engine (and alert rules) with a simulated clock.

    Prints the timeline, confirmed shifts and alerts; returns a summary with alert counts."""
    indices = indices or list(cfg["indices"])
    events, seeds = load_day(Path(day_dir), indices)
    engine = Engine(cfg, indices)
    day = events[0][0].date() if events else None
    rules = AlertEngine(cfg, AlertState(None, day)) if alerts and day else None
    for idx, candles in seeds.items():
        ok = engine.states[idx].seed_emas(candles)
        out(f"{idx}: EMA seeded from {len(candles)} recorded candles" if ok
            else f"{idx}: EMA seed failed ({len(candles)} candles) -> EMA None")
    tl = cfg["replay"]["timeline_minutes"]
    summary: dict[str, Any] = {"ticks": 0, "oi": 0, "shifts": [], "timeline": [], "alerts": [],
                               "alert_counts": {}}

    def handle(evs: list[dict[str, Any]], now: datetime | None = None, index: str | None = None) -> None:
        if rules and now is not None:
            keys = {e["index"] for e in evs} | ({index} if index else set())
            for k in sorted(keys):
                for a in rules.process(engine.states[k], [e for e in evs if e["index"] == k], now):
                    summary["alerts"].append(a)
                    summary["alert_counts"][a.type] = summary["alert_counts"].get(a.type, 0) + 1
                    out(f"{a.ts:%H:%M:%S} [{a.type}] {a.text}")
                    if on_alert:
                        on_alert(a)
        for ev in evs:
            if ev["type"] == "shift":
                line = f"{ev['ts']:%H:%M} {ev['index']:<9} SHIFT {ev['side']} {ev['old']:g} -> {ev['new']:g}"
                summary["shifts"].append(ev)
                out(line)
            elif ev["type"] == "candle" and ev["candle"].minutes == tl:
                line = timeline_line(engine.states[ev["index"]], ev["candle"].end)
                summary["timeline"].append(line)
                out(line)

    for ts, _o, kind, idx, payload in events:
        handle(engine.advance(ts), ts)
        if kind == "tick":
            summary["ticks"] += 1
            handle(engine.on_tick(idx, ts, payload), ts, idx)
        else:
            summary["oi"] += 1
            spot, snap = payload
            handle(engine.on_oi(idx, ts, snap, spot), ts, idx)
    if events:   # close the candle forming at the end of the recording (no flat fill beyond it)
        end = events[-1][0] + timedelta(minutes=tl)
        handle(engine.advance(end), end)
    summary["engine"] = engine
    return summary
