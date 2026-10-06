"""Run the engine on a recorded day with a simulated clock and print a timeline.

Timeline: wall/floor values, PCR and EMA50/EMA200 at every completed N-minute candle
(replay.timeline_minutes), plus confirmed wall/floor shifts as they happen.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

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
               out: Callable[[str], None] = print) -> dict[str, Any]:
    """Feed a recorded day through the engine; print the timeline; return a summary."""
    indices = indices or list(cfg["indices"])
    events, seeds = load_day(Path(day_dir), indices)
    engine = Engine(cfg, indices)
    for idx, candles in seeds.items():
        ok = engine.states[idx].seed_emas(candles)
        out(f"{idx}: EMA seeded from {len(candles)} recorded candles" if ok
            else f"{idx}: EMA seed failed ({len(candles)} candles) -> EMA None")
    tl = cfg["replay"]["timeline_minutes"]
    summary: dict[str, Any] = {"ticks": 0, "oi": 0, "shifts": [], "timeline": []}

    def handle(evs: list[dict[str, Any]]) -> None:
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
        handle(engine.advance(ts))
        if kind == "tick":
            summary["ticks"] += 1
            handle(engine.on_tick(idx, ts, payload))
        else:
            summary["oi"] += 1
            spot, snap = payload
            handle(engine.on_oi(idx, ts, snap, spot))
    if events:   # close the candle forming at the end of the recording (no flat fill beyond it)
        handle(engine.advance(events[-1][0] + timedelta(minutes=tl)))
    summary["engine"] = engine
    return summary
