"""Load config.yaml and .env; IST time helpers computed explicitly (never from OS timezone)."""
from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
ENV_PATH = ROOT / ".env"
ENV_KEYS = (
    "ANGEL_API_KEY",
    "ANGEL_CLIENT_CODE",
    "ANGEL_MPIN",
    "ANGEL_TOTP_SECRET",
    "TG_BOT_TOKEN",
    "TG_CHAT_ID",
)

# IST has no DST; a fixed offset avoids needing tzdata (missing on Windows/Termux).
IST = timezone(timedelta(minutes=330), name="IST")


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """Load config.yaml into a dict."""
    with Path(path or CONFIG_PATH).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_env(path: Path | str | None = None) -> dict[str, str]:
    """Load .env (if present) and return the credential keys; missing keys map to ''."""
    p = Path(path or ENV_PATH)
    if p.exists():
        load_dotenv(p, override=False)
    return {k: os.environ.get(k, "").strip() for k in ENV_KEYS}


def missing_env(env: dict[str, str], keys: tuple[str, ...] = ENV_KEYS) -> list[str]:
    """Return names of required keys that are empty (names only, never values)."""
    return [k for k in keys if not env.get(k)]


def now_ist() -> datetime:
    """Current time in IST (timezone-aware), derived from UTC."""
    return datetime.now(timezone.utc).astimezone(IST)


def today_ist() -> date:
    """Today's date in IST."""
    return now_ist().date()


def parse_hhmm(value: str) -> time:
    """Parse an 'HH:MM' string into a naive time."""
    hh, mm = str(value).split(":")
    return time(int(hh), int(mm))


def is_market_open(cfg: dict[str, Any], at: datetime | None = None) -> bool:
    """True on a weekday between schedule.market_open and schedule.market_close IST."""
    at = (at or now_ist()).astimezone(IST)
    if at.weekday() >= 5:
        return False
    sch = cfg["schedule"]
    return parse_hhmm(sch["market_open"]) <= at.time() <= parse_hhmm(sch["market_close"])
