from datetime import time

import pytest

from marketalert.walls import OiHistory, WallTracker, compute_walls, sum_snapshots

from .conftest import ist


def snap(ce, pe, strikes=(100, 200, 300, 400, 500, 600)):
    return {float(s): {"CE": c, "PE": p} for s, c, p in zip(strikes, ce, pe)}


def test_walls_floors_edge_excluded_and_pcr():
    s = snap(ce=[1, 2, 30, 40, 50, 99], pe=[99, 60, 70, 5, 4, 3])
    w = compute_walls(s, spot=330)
    assert w.wall.strike == 500 and w.wall2.strike == 400        # 600 (edge) excluded
    assert w.floor.strike == 300 and w.floor2.strike == 200      # 100 (edge) excluded
    assert w.excluded == [100, 600]
    assert w.pcr == pytest.approx(round(241 / 222, 3))


def test_wall_at_spot_counts_both_sides():
    w = compute_walls(snap(ce=[0, 0, 9, 1, 1, 0], pe=[0, 1, 9, 0, 0, 0]), spot=300)
    assert w.wall.strike == 300 and w.floor.strike == 300


def test_expiry_day_summing_changes_wall():
    cur = snap(ce=[0, 0, 0, 40, 30, 0], pe=[0, 5, 5, 0, 0, 0])
    nxt = snap(ce=[0, 0, 0, 0, 20, 0], pe=[0, 1, 1, 0, 0, 0])
    assert compute_walls(cur, 330).wall.strike == 400
    total = sum_snapshots([cur, nxt])
    assert total[500.0]["CE"] == 50
    assert compute_walls(total, 330).wall.strike == 500


def test_shift_needs_two_consecutive_polls():
    t = WallTracker(2)
    w = lambda strike: compute_walls(snap(ce=[0, 0, 0] + [9 if s == strike else 1 for s in (400, 500)] + [0],
                                          pe=[0, 9, 0, 0, 0, 0]), 330)
    assert t.update(w(400)) == []                 # first observation
    assert t.update(w(500)) == []                 # candidate 1
    assert t.update(w(400)) == []                 # back: candidate reset
    assert t.update(w(500)) == []
    assert t.update(w(500)) == [{"side": "wall", "old": 400, "new": 500}]
    assert t.confirmed["wall"] == 500


def test_oi_change_baseline_rules():
    h = OiHistory(30, time(9, 30))
    for (hh, mm), oi in {(9, 20): 50, (9, 30): 100, (9, 45): 110, (10, 0): 120, (10, 10): 130}.items():
        h.add(ist(2026, 10, 7, hh, mm), {500.0: {"CE": oi, "PE": 1}})
    assert h.oi_change_pct(500.0, "CE", ist(2026, 10, 7, 9, 50)) is None       # only 09:20 is 30 min old
    assert h.oi_change_pct(500.0, "CE", ist(2026, 10, 7, 10, 10)) == 30.0       # base 09:30
    assert h.oi_change_pct(500.0, "CE", ist(2026, 10, 7, 10, 20)) == pytest.approx(round(20 / 110 * 100, 2))
    assert h.oi_change_pct(999.0, "CE", ist(2026, 10, 7, 10, 10)) is None
