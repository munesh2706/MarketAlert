from datetime import timedelta

import pytest

from marketalert.indicators import EMA, EmaSet

from .conftest import ist

T0 = ist(2026, 10, 7, 9, 15)


def series(closes):
    return [(T0 + timedelta(minutes=15 * i), c) for i, c in enumerate(closes)]


def test_ema_hand_computed():
    e = EMA(3)                         # k = 0.5, seed = SMA(1,2,3) = 2
    assert e.seed(series([1, 2, 3, 4, 5]))
    assert e.value == pytest.approx(4.0)           # 2 -> 3 -> 4
    assert e.update(T0 + timedelta(minutes=75), 6) == pytest.approx(5.0)
    assert e.update(T0 + timedelta(minutes=60), 100) == pytest.approx(5.0)   # old candle ignored


def test_ema_multiplier():
    e = EMA(50)
    assert e.k == pytest.approx(2 / 51)


def test_unseeded_is_none_until_reseed():
    e = EMA(3)
    assert not e.seed(series([1, 2]))
    assert e.value is None
    assert e.update(T0 + timedelta(hours=1), 5) is None
    assert e.seed(series([1, 2, 3]))
    assert e.value == pytest.approx(2.0)


def test_emaset_partial():
    s = EmaSet([50, 200])
    assert not s.seed(series(list(range(100))))
    v = s.values()
    assert v[50] is not None and v[200] is None and not s.seeded
