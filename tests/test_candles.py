from datetime import time

from marketalert.candles import CandleBuilder, CandleSet

from .conftest import ist

OPEN, CLOSE = time(9, 15), time(15, 30)


def ohlc(c):
    return (f"{c.start:%H:%M}", c.open, c.high, c.low, c.close)


def test_one_minute_with_gap_carries_close():
    b = CandleBuilder(1, OPEN, CLOSE)
    assert b.on_tick(ist(2026, 10, 7, 9, 15, 10), 100) == []
    b.on_tick(ist(2026, 10, 7, 9, 15, 40), 102)
    assert [ohlc(c) for c in b.on_tick(ist(2026, 10, 7, 9, 16, 5), 101)] == [("09:15", 100, 102, 100, 102)]
    done = b.on_tick(ist(2026, 10, 7, 9, 19, 30), 105)
    assert [ohlc(c) for c in done] == [("09:16", 101, 101, 101, 101), ("09:17", 101, 101, 101, 101),
                                       ("09:18", 101, 101, 101, 101)]
    assert [c.ticks for c in done] == [1, 0, 0]
    assert ohlc(b.cur) == ("09:19", 105, 105, 105, 105)     # first real tick replaces carried close


def test_alignment_to_0915():
    s = CandleSet([1, 5, 15], OPEN, CLOSE)
    s.on_tick(ist(2026, 10, 7, 9, 21, 0), 10)
    assert f"{s.builders[5].cur.start:%H:%M}" == "09:20"
    assert f"{s.builders[15].cur.start:%H:%M}" == "09:15"
    done = s.on_tick(ist(2026, 10, 7, 9, 44, 59), 11)
    assert [ohlc(c) for c in done if c.minutes == 15] == [("09:15", 10, 10, 10, 10)]
    assert f"{s.builders[5].cur.start:%H:%M}" == "09:40"
    done = s.advance(ist(2026, 10, 7, 9, 45, 0))
    assert [ohlc(c) for c in done if c.minutes == 15] == [("09:30", 11, 11, 11, 11)]
    assert [c.minutes for c in done] == [1, 5, 15]


def test_preopen_ignored_and_session_end():
    b = CandleBuilder(5, OPEN, CLOSE)
    assert b.on_tick(ist(2026, 10, 7, 9, 10), 99) == [] and b.cur is None
    b.on_tick(ist(2026, 10, 7, 15, 27), 200)
    done = b.advance(ist(2026, 10, 7, 15, 45))
    assert [ohlc(c) for c in done] == [("15:25", 200, 200, 200, 200)]
    assert b.cur is None
    # next day starts fresh, no overnight fill
    assert b.on_tick(ist(2026, 10, 8, 9, 16), 210) == []
    assert f"{b.cur.start:%d %H:%M}" == "08 09:15"
