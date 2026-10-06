"""Daily timeline and holidays.

08:50 refresh -> 09:00 start -> 09:20 holiday check -> 09:45 market map -> (alerts until 14:45,
enforced by AlertEngine) -> 15:45 summary -> sleep until the next trading day 08:50.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Protocol

from .config import IST, parse_hhmm


class Schedule:
    """Trading-day calendar and event times (IST)."""

    def __init__(self, cfg: dict[str, Any], holidays: dict[str, set[date]] | None = None):
        s = cfg["schedule"]
        self.cfg = s
        self.holidays = holidays or {}
        self.exchanges = sorted({ix["spot_exchange"] for ix in cfg["indices"].values()})
        self.times = {name: parse_hhmm(s[key]) for name, key in (
            ("refresh", "refresh"), ("start", "start"), ("holiday_check", "holiday_check"),
            ("map", "market_map"), ("summary", "summary"))}
        self.grace = timedelta(seconds=s["holiday_check_grace_seconds"])

    def is_trading_day(self, d: date) -> bool:
        """Weekday, and not a holiday on every exchange we track."""
        if self.cfg["skip_weekends"] and d.weekday() >= 5:
            return False
        return not all(d in self.holidays.get(ex, set()) for ex in self.exchanges)

    def at(self, d: date, name: str) -> datetime:
        return datetime.combine(d, self.times[name], IST)

    def next_start(self, now: datetime) -> datetime:
        """When the next trading day begins (refresh time). In the past = start now (late start)."""
        d = now.astimezone(IST).date()
        if self.is_trading_day(d) and now < self.at(d, "summary"):
            return self.at(d, "refresh")
        d += timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return self.at(d, "refresh")

    def due(self, now: datetime, done: set[str], feed_started: datetime | None) -> list[str]:
        """Events due now, in order. holiday_check and map also wait `grace` after the feed starts
        (so a late start has ticks and a first OI poll before judging / mapping)."""
        d = now.astimezone(IST).date()
        settled = feed_started is not None and now >= feed_started + self.grace
        out = []
        for name in ("refresh", "start", "holiday_check", "map", "summary"):
            if name in done or now < self.at(d, name):
                continue
            if name in ("holiday_check", "map") and not settled:
                continue
            if name != "refresh" and "refresh" not in done and "refresh" not in out:
                continue
            out.append(name)
        return out


class DayActions(Protocol):
    def refresh(self) -> None: ...
    def start(self) -> None: ...
    def has_ticks(self) -> bool: ...
    def holiday(self) -> None: ...
    def market_map(self) -> None: ...
    def summary(self) -> None: ...
    def stop(self) -> None: ...
    def tick(self, now: datetime) -> None: ...


class DayRunner:
    """Drives one trading day; call step(now) every loop_seconds until it returns True."""

    def __init__(self, sched: Schedule, actions: DayActions):
        self.sched, self.a = sched, actions
        self.done: set[str] = set()
        self.feed_started: datetime | None = None
        self.result: str | None = None          # "summary" | "holiday"

    def step(self, now: datetime) -> bool:
        for ev in self.sched.due(now, self.done, self.feed_started):
            self.done.add(ev)
            if ev == "refresh":
                self.a.refresh()
            elif ev == "start":
                self.a.start()
                self.feed_started = now
            elif ev == "holiday_check":
                if not self.a.has_ticks():
                    self.a.holiday()
                    self.a.stop()
                    self.result = "holiday"
                    return True
            elif ev == "map":
                self.a.market_map()
            elif ev == "summary":
                self.a.summary()
                self.a.stop()
                self.result = "summary"
                return True
        if "start" in self.done:
            self.a.tick(now)
        return False
