"""Run a live MarketAlert session (M1: engine + recording; no alerts/Telegram yet).

Usage:
  python scripts/run.py --minutes 60      # live session, records to data/recordings/<date>/
  python scripts/run.py --smoke           # login, EMA seed/cache, one OI poll per index, exit
  python scripts/run.py --minutes 5 --no-record
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from marketalert.config import load_config, load_env, missing_env  # noqa: E402
from marketalert.main import LiveSession, setup_logging  # noqa: E402

ANGEL_KEYS = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_MPIN", "ANGEL_TOTP_SECRET")


def main() -> int:
    ap = argparse.ArgumentParser(description="MarketAlert live session")
    ap.add_argument("--minutes", type=float, default=60, help="session length (default 60)")
    ap.add_argument("--smoke", action="store_true", help="one-shot smoke test, then exit")
    ap.add_argument("--no-record", action="store_true", help="do not write recordings")
    a = ap.parse_args()
    cfg = load_config()
    env = load_env()
    miss = missing_env(env, ANGEL_KEYS)
    if miss:
        print("Missing in .env: " + ", ".join(miss))
        return 2
    setup_logging(cfg)
    session = LiveSession(cfg, env, record=not a.no_record)
    if a.smoke:
        print(json.dumps(session.smoke(), indent=1, default=str))
    else:
        session.run(a.minutes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
