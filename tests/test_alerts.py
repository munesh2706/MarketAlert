import copy
from datetime import date, timedelta

import pytest

from marketalert.alerts import AlertEngine, AlertState, build_map, build_summary, fnum, foi, record_map_walls
from marketalert.candles import Candle
from marketalert.engine import IndexState

from .conftest import ist

STRIKES = range(24800, 25500, 100)          # 24800 and 25400 are the excluded edges
DAY = date(2026, 10, 7)


def snap(wall=25200, floor=25000, wall_oi=5_000_000, floor_oi=12_400_000):
    return {float(s): {"CE": wall_oi if s == wall else 100_000, "PE": floor_oi if s == floor else 100_000}
            for s in STRIKES}


@pytest.fixture
def env(cfg):
    st = IndexState("NIFTY", cfg)
    eng = AlertEngine(cfg, AlertState(None, DAY))
    return cfg, st, eng


def oi(st, eng, t, spot, **kw):
    return eng.process(st, st.on_oi(t, snap(**kw), spot), t)


def at(st, eng, t, spot):
    st.spot = spot
    return eng.process(st, [], t)


def types(alerts):
    return [a.type for a in alerts]


def test_zone_once_then_rearm_after_reset_gap(env):
    _, st, eng = env
    t = ist(2026, 10, 7, 10, 0)
    a = oi(st, eng, t, 25008)
    assert types(a) == ["ZONE"]
    assert a[0].text.startswith("🟢 NIFTY near FLOOR 25,000 | Spot 25,008 (8 pts) | PE OI 1.24 Cr")
    assert "2nd floor" in a[0].text
    assert at(st, eng, t, 25005) == []                 # disarmed
    assert at(st, eng, t, 25025) == []                 # 25 < reset gap 30
    assert at(st, eng, t, 25031) == []                 # re-armed (31 away), outside zone
    assert types(at(st, eng, t, 25009)) == ["ZONE"]


def test_zone_rearm_on_level_change(env):
    _, st, eng = env
    t = ist(2026, 10, 7, 10, 0)
    assert types(oi(st, eng, t, 25008)) == ["ZONE"]
    assert oi(st, eng, t, 25008, floor=24900) == []        # floor strike changed -> 25000 key re-armed
    assert types(oi(st, eng, t, 25008, floor=25000)) == ["ZONE"]


def test_window_boundaries(env):
    _, st, eng = env
    assert oi(st, eng, ist(2026, 10, 7, 9, 44, 59), 25008) == []
    assert types(at(st, eng, ist(2026, 10, 7, 9, 45), 25008)) == ["ZONE"]
    assert types(at(st, eng, ist(2026, 10, 7, 14, 44, 59), 25195)) == ["ZONE"]     # wall zone
    assert at(st, eng, ist(2026, 10, 7, 14, 45), 25200) == []


def test_daily_cap(cfg):
    cfg = copy.deepcopy(cfg)
    cfg["alerts"]["max_alerts_per_index"] = 2
    st, eng = IndexState("NIFTY", cfg), AlertEngine(cfg, AlertState(None, DAY))
    t = ist(2026, 10, 7, 10, 0)
    assert types(oi(st, eng, t, 25008)) == ["ZONE"]
    assert types(at(st, eng, t, 25195)) == ["ZONE"]
    assert at(st, eng, t, 25100) == [] and at(st, eng, t, 25005) == []    # re-armed but capped
    assert eng.count("NIFTY") == 2


def test_mute_unmute(env):
    _, st, eng = env
    t = ist(2026, 10, 7, 10, 0)
    assert eng.mute("all", 30, t) == ["NIFTY", "BANKNIFTY", "SENSEX"]
    assert oi(st, eng, t, 25008) == []
    assert eng.muted("NIFTY", t + timedelta(minutes=29)) and not eng.muted("NIFTY", t + timedelta(minutes=30))
    eng.unmute("NIFTY")
    assert types(at(st, eng, t, 25008)) == ["ZONE"]    # not disarmed while muted


def test_state_reload_same_day_and_reset_next_day(cfg, tmp_path):
    path = tmp_path / "state.json"
    st, eng = IndexState("NIFTY", cfg), AlertEngine(cfg, AlertState(path, DAY))
    t = ist(2026, 10, 7, 10, 0)
    eng.state.data["tg_offset"] = 77
    assert types(oi(st, eng, t, 25008)) == ["ZONE"]
    eng.mute("SENSEX", 60, t)
    # restart same day: still disarmed, counts and mutes kept
    eng2 = AlertEngine(cfg, AlertState(path, DAY))
    assert eng2.count("NIFTY") == 1 and eng2.muted("SENSEX", t)
    assert eng2.process(st, [], t) == []
    assert eng2.state["last_walls"]["NIFTY"]["floor"] == 25000
    # next day: reset, Telegram offset kept
    s3 = AlertState(path, DAY + timedelta(days=1))
    assert s3["arms"] == {} and s3["counts"] == {} and s3["mutes"] == {} and s3["tg_offset"] == 77


def test_ema_side_confluence_rearm_and_none(env):
    _, st, eng = env
    t = ist(2026, 10, 7, 10, 0)
    st.on_oi(t, snap(wall=25100, floor=24900), 25050)
    st.emas.emas[50].value = 25100.0                       # EMA200 stays None -> skipped
    a = [x for x in at(st, eng, t, 25110) if x.type == "EMA"]   # ZONE (wall 25,100) also fires
    assert len(a) == 1
    assert a[0].text.startswith("📈 NIFTY near 15m EMA50 from above | Spot 25,110 | EMA50 25,100 (10 pts)")
    assert "⚡ Wall 25,100" in a[0].text
    assert "EMA" not in types(at(st, eng, t, 25095))       # disarmed
    assert "EMA" not in types(at(st, eng, t, 25131))       # re-arm: 31 from current EMA
    b = [x for x in at(st, eng, t, 25092) if x.type == "EMA"]
    assert b[0].text.startswith("📉 NIFTY near 15m EMA50 from below")


def c5(h, m, close):
    return Candle(ist(2026, 10, 7, h, m), 5, close, close, close, close, 1)


def bo(st, eng, candle):
    t = candle.end
    return eng.process(st, [{"type": "candle", "index": "NIFTY", "candle": candle}], t)


def test_breakout_then_failed_within_three(env):
    _, st, eng = env
    oi(st, eng, ist(2026, 10, 7, 9, 50), 25190)            # wall 25200 (ZONE fires here)
    st.spot = 25206
    a = bo(st, eng, c5(10, 0, 25206))
    assert "BREAKOUT" in types(a)
    text = [x for x in a if x.type == "BREAKOUT"][0].text
    assert text.startswith("🚀 NIFTY BREAKOUT above WALL 25,200 | 5m close 25,206 (+6 pts)")
    assert bo(st, eng, c5(10, 5, 25210)) == []              # 1st after: still outside
    st.spot = 25190
    f = [x for x in bo(st, eng, c5(10, 10, 25190)) if x.type == "BREAKOUT_FAILED"]
    assert len(f) == 1 and "back inside (10 pts below)" in f[0].text
    assert eng.state["pending"] == []
    assert [x for x in bo(st, eng, c5(10, 15, 25180)) if x.type == "BREAKOUT_FAILED"] == []   # once only


def test_breakout_failed_window_expires_and_rearm(env):
    _, st, eng = env
    oi(st, eng, ist(2026, 10, 7, 9, 50), 25190)
    st.spot = 25210
    assert "BREAKOUT" in types(bo(st, eng, c5(10, 0, 25206)))
    for m, close in ((5, 25210), (10, 25215), (15, 25220)):   # 3 candles outside
        bo(st, eng, c5(10, m, close))
    assert eng.state["pending"] == []
    assert "BREAKOUT_FAILED" not in types(bo(st, eng, c5(10, 20, 25190)))
    assert "BREAKOUT" not in types(bo(st, eng, c5(10, 25, 25207)))   # disarmed
    st.spot = 25170                                            # 30 back below the level
    eng.process(st, [], ist(2026, 10, 7, 10, 31))
    st.spot = 25208
    assert "BREAKOUT" in types(bo(st, eng, c5(10, 30, 25208)))


def test_wall_shift_alert(env):
    _, st, eng = env
    t = ist(2026, 10, 7, 10, 0)
    oi(st, eng, t, 25100)
    assert oi(st, eng, t + timedelta(minutes=3), 25100, wall=25300) == []
    a = [x for x in oi(st, eng, t + timedelta(minutes=6), 25100, wall=25300) if x.type == "WALL_SHIFT"]
    assert len(a) == 1
    assert a[0].text.startswith("🔀 NIFTY WALL SHIFT 25,200 → 25,300 | CE OI 1.0 L → 50.0 L | Spot 25,100 (200 pts")


def test_oi_change_once_per_30_min(env):
    _, st, eng = env
    t0 = ist(2026, 10, 7, 9, 50)
    oi(st, eng, t0, 25100, wall_oi=1_000_000)
    a = [x for x in oi(st, eng, t0 + timedelta(minutes=30), 25100, wall_oi=1_200_000) if x.type == "OI_CHANGE"]
    assert len(a) == 1
    assert a[0].text.startswith("📊 NIFTY WALL 25,200 CE OI +20% in 30m (writers adding) | OI 12.0 L")
    assert not [x for x in oi(st, eng, t0 + timedelta(minutes=33), 25100, wall_oi=1_250_000)
                if x.type == "OI_CHANGE"]
    b = [x for x in oi(st, eng, t0 + timedelta(minutes=61), 25100, wall_oi=900_000) if x.type == "OI_CHANGE"]
    assert len(b) == 1 and "writers leaving" in b[0].text


def test_formatting_units():
    assert foi(12_400_000) == "1.24 Cr" and foi(3_320_000) == "33.2 L" and foi(None) == "-"
    assert fnum(25008.4) == "25,008" and fnum(None) == "-"


def test_map_and_summary(cfg):
    st = IndexState("NIFTY", cfg)
    eng = AlertEngine(cfg, AlertState(None, DAY))
    t = ist(2026, 10, 7, 9, 45)
    st.on_tick(ist(2026, 10, 7, 9, 15), 25000.0)
    st.on_oi(t, snap(), 25050)
    st.emas.emas[50].value, st.emas.emas[200].value = 25010.0, 24900.0
    record_map_walls({"NIFTY": st}, eng)
    m = build_map({"NIFTY": st}, {"NIFTY": {"expiry": DAY, "note": "expiry day: current + next summed"}}, t)
    assert m.startswith("NIFTY 25,000 | Exp 07-Oct (expiry day: current + next summed)")
    assert "Walls 25,200 (50.0 L)" in m and "Floors 25,000 (1.24 Cr)" in m and "EMA200 24,900" in m
    st.on_tick(ist(2026, 10, 7, 15, 0), 25100.0)
    st.on_oi(ist(2026, 10, 7, 15, 0), snap(wall=25300), 25100)
    s = build_summary({"NIFTY": st}, eng, ist(2026, 10, 7, 15, 45))
    assert "NIFTY O 25,000 H 25,100 L 25,000 C 25,100" in s
    assert "Wall 25,200 → 25,300" in s and "Floor 25,000 → 25,000" in s and "Alerts 0" in s
    assert "OI Δ: 25,200 CE -49.0 L (-98%), 25,300 CE +49.0 L" in s
