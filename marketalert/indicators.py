"""EMA in pure Python (multiplier 2/(n+1), seeded with the SMA of the first n closes)."""
from __future__ import annotations

from datetime import datetime


class EMA:
    """EMA updated only on completed candles. value is None until seeded with >= n closes."""

    def __init__(self, period: int):
        self.period = period
        self.k = 2 / (period + 1)
        self.value: float | None = None
        self.last_ts: datetime | None = None

    def seed(self, candles: list[tuple[datetime, float]]) -> bool:
        """Seed from (candle_start, close) pairs in time order. Returns False if too few."""
        if len(candles) < self.period:
            self.value, self.last_ts = None, None
            return False
        closes = [c for _, c in candles]
        v = sum(closes[: self.period]) / self.period
        for c in closes[self.period:]:
            v = c * self.k + v * (1 - self.k)
        self.value, self.last_ts = v, candles[-1][0]
        return True

    def update(self, ts: datetime, close: float) -> float | None:
        """Apply one completed candle; ignored while unseeded or if ts <= last seeded candle."""
        if self.value is None or (self.last_ts is not None and ts <= self.last_ts):
            return self.value
        self.value = close * self.k + self.value * (1 - self.k)
        self.last_ts = ts
        return self.value


class EmaSet:
    """EMAs of several periods for one index (e.g. EMA50 / EMA200 on 15-min closes)."""

    def __init__(self, periods: list[int]):
        self.emas = {p: EMA(p) for p in periods}

    @property
    def seeded(self) -> bool:
        return all(e.value is not None for e in self.emas.values())

    def seed(self, candles: list[tuple[datetime, float]]) -> bool:
        return all([e.seed(candles) for e in self.emas.values()])

    def update(self, ts: datetime, close: float) -> None:
        for e in self.emas.values():
            e.update(ts, close)

    def values(self) -> dict[int, float | None]:
        return {p: e.value for p, e in self.emas.items()}
