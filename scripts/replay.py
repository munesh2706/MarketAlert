"""Replay a recorded day through the engine and alert rules (simulated clock).

Prints the timeline, confirmed shifts and the alerts that would have been sent.
--telegram also sends the alerts, prefixed "[REPLAY]".

Usage: python scripts/replay.py 2026-10-07 [--index NIFTY] [--dir data/recordings] [--telegram]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from marketalert.config import ROOT, load_config, load_env  # noqa: E402
from marketalert.replay import replay_day  # noqa: E402


def main() -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Replay a recorded day")
    ap.add_argument("day", help="YYYY-MM-DD")
    ap.add_argument("--index", action="append", choices=list(cfg["indices"]), help="repeatable")
    ap.add_argument("--dir", default=cfg["paths"]["recordings_dir"])
    ap.add_argument("--telegram", action="store_true", help="send alerts to Telegram as [REPLAY]")
    a = ap.parse_args()
    day_dir = (ROOT / a.dir / a.day) if not Path(a.dir).is_absolute() else Path(a.dir) / a.day
    if not day_dir.exists():
        print(f"no recording at {day_dir}")
        return 1
    on_alert = None
    if a.telegram:
        from marketalert.telegram import TelegramClient

        env = load_env()
        tg = TelegramClient(env["TG_BOT_TOKEN"], env["TG_CHAT_ID"], cfg)
        gap = cfg["telegram"]["min_send_interval_seconds"]

        def on_alert(alert):  # noqa: E306 - sequential sends respect the rate limit
            tg.send_now(f"[REPLAY] {alert.ts:%H:%M} {alert.text}")
            time.sleep(gap)

    s = replay_day(cfg, day_dir, a.index, on_alert=on_alert)
    counts = ", ".join(f"{k} {v}" for k, v in sorted(s["alert_counts"].items())) or "none"
    print(f"-- {s['ticks']} ticks, {s['oi']} OI snapshots, {len(s['shifts'])} confirmed shifts, "
          f"{len(s['alerts'])} alerts ({counts})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
