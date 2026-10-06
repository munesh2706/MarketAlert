"""Save live ticks and OI snapshots to data/recordings/YYYY-MM-DD/ as compact JSON lines.

Files per index: {index}_ticks.jsonl  -> [epoch_s, price]
                 {index}_oi.jsonl     -> {"t", "spot", "exp", "oi": {strike: [CE, PE]}}
                 {index}_seed.json    -> [[candle_start_iso, close], ...] (EMA seed used live)
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime
from pathlib import Path
from typing import IO, Any

from .instruments import write_json_atomic


class Recorder:
    """Append-only, thread-safe, line-buffered writer; switches folder when the IST date changes."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._files: dict[tuple[date, str], IO[str]] = {}
        self._lock = threading.Lock()

    def day_dir(self, d: date) -> Path:
        p = self.root / d.isoformat()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _write(self, d: date, name: str, obj: Any) -> None:
        with self._lock:
            key = (d, name)
            if key not in self._files:
                for k in [k for k in self._files if k[0] != d]:   # day changed: close old files
                    self._files.pop(k).close()
                self._files[key] = (self.day_dir(d) / name).open("a", encoding="utf-8", buffering=1)
            self._files[key].write(json.dumps(obj, separators=(",", ":")) + "\n")

    def tick(self, index: str, ts: datetime, price: float) -> None:
        self._write(ts.date(), f"{index}_ticks.jsonl", [round(ts.timestamp(), 3), price])

    def oi(self, index: str, ts: datetime, spot: float | None, expiries: list[date],
           snap: dict[float, dict[str, float | None]]) -> None:
        self._write(ts.date(), f"{index}_oi.jsonl", {
            "t": round(ts.timestamp(), 3), "spot": spot, "exp": [e.isoformat() for e in expiries],
            "oi": {f"{k:g}": [v.get("CE"), v.get("PE")] for k, v in sorted(snap.items())}})

    def seed(self, index: str, d: date, candles: list[tuple[datetime, float]]) -> None:
        write_json_atomic(self.day_dir(d) / f"{index}_seed.json",
                          [[t.isoformat(), c] for t, c in candles])

    def close(self) -> None:
        with self._lock:
            for f in self._files.values():
                f.close()
            self._files.clear()
