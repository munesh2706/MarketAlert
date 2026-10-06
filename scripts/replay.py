"""Replay a recorded day through the engine and print the timeline (no Telegram).

Usage: python scripts/replay.py 2026-10-07 [--index NIFTY] [--dir data/recordings]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from marketalert.config import ROOT, load_config  # noqa: E402
from marketalert.replay import replay_day  # noqa: E402


def main() -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Replay a recorded day")
    ap.add_argument("day", help="YYYY-MM-DD")
    ap.add_argument("--index", action="append", choices=list(cfg["indices"]), help="repeatable")
    ap.add_argument("--dir", default=cfg["paths"]["recordings_dir"])
    a = ap.parse_args()
    day_dir = (ROOT / a.dir / a.day) if not Path(a.dir).is_absolute() else Path(a.dir) / a.day
    if not day_dir.exists():
        print(f"no recording at {day_dir}")
        return 1
    s = replay_day(cfg, day_dir, a.index)
    print(f"-- {s['ticks']} ticks, {s['oi']} OI snapshots, {len(s['shifts'])} confirmed shifts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
