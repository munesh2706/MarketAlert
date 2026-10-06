"""Run MarketAlert.

Usage:
  python scripts/run.py                   # full bot: runs forever on the daily schedule
  python scripts/run.py --smoke           # login, EMA seed/cache, one OI poll per index, exit
  python scripts/run.py --minutes 60      # unscheduled session (recording/testing), alerts to log only
  python scripts/run.py --tg-test         # send one Telegram test message and exit
  add --no-record to skip writing data/recordings/
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from marketalert.config import load_config, load_env, missing_env  # noqa: E402
from marketalert.main import Bot, LiveSession, setup_logging  # noqa: E402

ANGEL_KEYS = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_MPIN", "ANGEL_TOTP_SECRET")
TG_KEYS = ("TG_BOT_TOKEN", "TG_CHAT_ID")


def main() -> int:
    ap = argparse.ArgumentParser(description="MarketAlert")
    ap.add_argument("--smoke", action="store_true", help="one-shot smoke test, then exit")
    ap.add_argument("--minutes", type=float, help="unscheduled session of N minutes (no Telegram)")
    ap.add_argument("--tg-test", action="store_true", help="send a Telegram test message, then exit")
    ap.add_argument("--no-record", action="store_true", help="do not write recordings")
    a = ap.parse_args()
    cfg = load_config()
    env = load_env()
    need = TG_KEYS if a.tg_test else ANGEL_KEYS if (a.smoke or a.minutes) else ANGEL_KEYS + TG_KEYS
    miss = missing_env(env, need)
    if miss:
        print("Missing in .env: " + ", ".join(miss))
        return 2
    setup_logging(cfg)
    if a.tg_test:
        from marketalert.telegram import TelegramClient

        ok = TelegramClient(env["TG_BOT_TOKEN"], env["TG_CHAT_ID"], cfg).send_now("🧪 MarketAlert M2 test")
        print("telegram test:", "sent" if ok else "FAILED (see logs/marketalert.log)")
        return 0 if ok else 1
    if a.smoke:
        print(json.dumps(LiveSession(cfg, env, record=not a.no_record).smoke(), indent=1, default=str))
        return 0
    if a.minutes:
        LiveSession(cfg, env, record=not a.no_record).run(a.minutes)
        return 0
    bot = Bot(cfg, env, record=not a.no_record)
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        print("stopping...")
    finally:
        bot.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
