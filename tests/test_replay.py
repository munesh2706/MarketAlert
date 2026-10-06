from datetime import date, timedelta

from marketalert.recorder import Recorder
from marketalert.replay import replay_day

from .conftest import ist

STRIKES = [22400 + 50 * i for i in range(9)]


def oi_snap(wall):
    return {float(s): {"CE": 900 if s == wall else 100, "PE": 800 if s == 22550 else 50} for s in STRIKES}


def test_record_replay_round_trip(cfg, tmp_path):
    rec = Recorder(tmp_path)
    day = date(2026, 10, 7)
    seed = [(ist(2026, 10, 1, 9, 15) + timedelta(minutes=15 * i), 22500.0) for i in range(250)]
    rec.seed("NIFTY", day, seed)
    t = ist(2026, 10, 7, 9, 15)
    n_ticks = 0
    while t <= ist(2026, 10, 7, 10, 15):
        rec.tick("NIFTY", t, 22600 + (n_ticks % 7))
        n_ticks += 1
        t += timedelta(seconds=30)
    t = ist(2026, 10, 7, 9, 18)
    n_oi = 0
    while t <= ist(2026, 10, 7, 10, 12):
        rec.oi("NIFTY", t, 22603.0, [day], oi_snap(22700 if t < ist(2026, 10, 7, 9, 40) else 22750))
        n_oi += 1
        t += timedelta(minutes=3)
    rec.close()

    lines = []
    s = replay_day(cfg, tmp_path / "2026-10-07", ["NIFTY"], out=lines.append)
    assert s["ticks"] == n_ticks and s["oi"] == n_oi
    assert [(e["side"], e["old"], e["new"], f"{e['ts']:%H:%M}") for e in s["shifts"]] == \
        [("wall", 22700, 22750, "09:45")]                  # 09:42 candidate, 09:45 confirmed
    assert [ln[:5] for ln in s["timeline"]] == ["09:30", "09:45", "10:00", "10:15", "10:30"]
    st = s["engine"].states["NIFTY"]
    assert st.walls.wall.strike == 22750 and st.walls.floor.strike == 22550
    assert all(v is not None for v in st.emas.values().values())
    assert "EMA50=" in s["timeline"][-1] and "wall 22750" in s["timeline"][-1]
    assert st.snapshot == oi_snap(22750)
    assert lines[0].startswith("NIFTY: EMA seeded from 250")
