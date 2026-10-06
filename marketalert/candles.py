"""Build 1/5/15-minute candles from (ts, price) ticks, aligned to the session open (09:15 IST).

Completed candles are emitted when a later tick arrives or when advance(now) passes the
candle end. Minutes with no ticks become flat candles at the previous close. Candles never
start at/after the session close; a new trading day starts fresh (no overnight fill).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from .config import IST


@dataclass
class Candle:
    start: datetime
    minutes: int
    open: float
    high: float
    low: float
    close: float
    ticks: int = 0

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.minutes)

    def add(self, price: float) -> None:
        if self.ticks == 0:               # first real tick replaces the carried close
            self.open = self.high = self.low = self.close = price
            self.ticks = 1
            return
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.ticks += 1


class CandleBuilder:
    """Candles of one timeframe for one instrument."""

    def __init__(self, minutes: int, session_open: time, session_close: time):
        self.minutes = minutes
        self.open_t = session_open
        self.close_t = session_close
        self.cur: Candle | None = None

    def _bounds(self, ts: datetime) -> tuple[datetime, datetime]:
        d = ts.date()
        return (datetime.combine(d, self.open_t, IST), datetime.combine(d, self.close_t, IST))

    def _bucket(self, ts: datetime) -> datetime:
        start, _ = self._bounds(ts)
        n = int((ts - start).total_seconds() // (self.minutes * 60))
        return start + timedelta(minutes=n * self.minutes)

    def advance(self, now: datetime) -> list[Candle]:
        """Emit candles whose end <= now, filling tick-less buckets with the previous close."""
        now = now.astimezone(IST)
        out: list[Candle] = []
        while self.cur and self.cur.end <= now:
            done = self.cur
            out.append(done)
            _, close = self._bounds(done.start)
            if done.end >= close:
                self.cur = None
                break
            c = done.close
            self.cur = Candle(done.end, self.minutes, c, c, c, c)   # tick-less placeholder
        return out

    def on_tick(self, ts: datetime, price: float) -> list[Candle]:
        """Add a tick; return candles completed before it."""
        ts = ts.astimezone(IST)
        open_dt, close_dt = self._bounds(ts)
        out: list[Candle] = []
        if self.cur and self.cur.start.date() != ts.date():
            out.append(self.cur)          # previous day's last candle (should not normally occur)
            self.cur = None
        out += self.advance(ts)
        if not open_dt <= ts < close_dt:
            return out
        if self.cur is None:
            b = self._bucket(ts)
            self.cur = Candle(b, self.minutes, price, price, price, price, 1)
        else:
            self.cur.add(price)
        return out


class CandleSet:
    """1-, 5- and 15-minute builders for one index."""

    def __init__(self, timeframes: list[int], session_open: time, session_close: time):
        self.builders = {tf: CandleBuilder(tf, session_open, session_close) for tf in timeframes}

    def on_tick(self, ts: datetime, price: float) -> list[Candle]:
        out: list[Candle] = []
        for b in self.builders.values():
            out += b.on_tick(ts, price)
        return out

    def advance(self, now: datetime) -> list[Candle]:
        out: list[Candle] = []
        for b in self.builders.values():
            out += b.advance(now)
        return out
