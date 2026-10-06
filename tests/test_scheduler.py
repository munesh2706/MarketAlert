from datetime import date, timedelta

from marketalert.scheduler import DayRunner, Schedule

from .conftest import ist


def test_trading_days(cfg):
    s = Schedule(cfg, {"NSE": {date(2026, 10, 2)}, "BSE": {date(2026, 10, 2)}})
    assert s.is_trading_day(date(2026, 10, 6))
    assert not s.is_trading_day(date(2026, 10, 3))           # Saturday
    assert not s.is_trading_day(date(2026, 10, 2))           # holiday on both exchanges
    s2 = Schedule(cfg, {"NSE": {date(2026, 10, 2)}})
    assert s2.is_trading_day(date(2026, 10, 2))              # BSE still open (SENSEX)


def test_next_start(cfg):
    s = Schedule(cfg, {"NSE": {date(2026, 10, 12)}, "BSE": {date(2026, 10, 12)}})
    assert s.next_start(ist(2026, 10, 9, 7, 0)) == ist(2026, 10, 9, 8, 50)
    assert s.next_start(ist(2026, 10, 9, 11, 0)) == ist(2026, 10, 9, 8, 50)    # past -> start now
    assert s.next_start(ist(2026, 10, 9, 16, 0)) == ist(2026, 10, 13, 8, 50)   # Fri -> skip weekend + Mon holiday
    assert s.next_start(ist(2026, 10, 10, 12, 0)) == ist(2026, 10, 13, 8, 50)  # Saturday


class Actions:
    def __init__(self, ticks=True):
        self.calls, self.ticks, self.ticked = [], ticks, 0
        self.now = None

    def _rec(self, name):
        self.calls.append((name, f"{self.now:%H:%M}"))

    def refresh(self): self._rec("refresh")
    def start(self): self._rec("start")
    def has_ticks(self): return self.ticks
    def holiday(self): self._rec("holiday")
    def market_map(self): self._rec("map")
    def summary(self): self._rec("summary")
    def stop(self): self._rec("stop")
    def tick(self, now): self.ticked += 1


def run_day(cfg, start, actions, end_h=16):
    r = DayRunner(Schedule(cfg), actions)
    t = start
    while t < start.replace(hour=end_h):
        actions.now = t
        if r.step(t):
            return r
        t += timedelta(seconds=30)
    return r


def test_full_day_timeline(cfg):
    a = Actions()
    r = run_day(cfg, ist(2026, 10, 7, 8, 45), a)
    assert a.calls == [("refresh", "08:50"), ("start", "09:00"), ("map", "09:45"),
                       ("summary", "15:45"), ("stop", "15:45")]
    assert r.result == "summary" and a.ticked > 500


def test_no_ticks_means_holiday(cfg):
    a = Actions(ticks=False)
    r = run_day(cfg, ist(2026, 10, 7, 8, 45), a)
    assert a.calls == [("refresh", "08:50"), ("start", "09:00"), ("holiday", "09:20"), ("stop", "09:20")]
    assert r.result == "holiday"


def test_late_start_waits_grace_before_map(cfg):
    a = Actions()
    run_day(cfg, ist(2026, 10, 7, 11, 0), a, end_h=12)
    assert a.calls[:3] == [("refresh", "11:00"), ("start", "11:00"), ("map", "11:02")]
