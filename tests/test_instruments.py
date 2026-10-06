import json
from datetime import date
from types import SimpleNamespace

from marketalert.instruments import Instruments, ensure_instruments, load_holidays, wait_for_instruments


def test_expiry_day_and_oi_expiries(cfg, filtered):
    inst = Instruments(filtered, cfg)
    assert inst.is_expiry_day("NIFTY", date(2026, 10, 6))
    assert not inst.is_expiry_day("NIFTY", date(2026, 10, 7))
    assert inst.oi_expiries("NIFTY", date(2026, 10, 6)) == [date(2026, 10, 6), date(2026, 10, 13)]
    assert inst.oi_expiries("NIFTY", date(2026, 10, 7)) == [date(2026, 10, 13)]
    assert inst.oi_expiries("SENSEX", date(2026, 10, 7)) == [date(2026, 10, 8)]
    assert inst.spot_token("SENSEX") == "99919000"


def test_bnf_rollover_regular(cfg, filtered):
    inst = Instruments(filtered, cfg)
    # expiry Tue 27 Oct: last 3 sessions = Fri 23, Mon 26, Tue 27
    assert [inst.in_bnf_rollover(date(2026, 10, d)) for d in (22, 23, 26, 27)] == [False, True, True, True]
    assert not inst.in_bnf_rollover(date(2026, 10, 24))          # Saturday
    assert inst.oi_expiries("BANKNIFTY", date(2026, 10, 23)) == [date(2026, 10, 27), date(2026, 11, 23)]
    assert inst.oi_expiries("BANKNIFTY", date(2026, 10, 22)) == [date(2026, 10, 27)]


def test_bnf_rollover_with_holiday_in_window(cfg, filtered):
    inst = Instruments(filtered, cfg, {"NSE": {date(2026, 10, 26)}})
    # Mon 26 is a holiday -> sessions Thu 22, Fri 23, Tue 27
    assert [inst.in_bnf_rollover(date(2026, 10, d)) for d in (21, 22, 23, 27)] == [False, True, True, True]


def test_holiday_shifted_expiry(cfg, filtered, tmp_path):
    p = tmp_path / "h.yaml"
    p.write_text("NSE: [2026-11-24]\nBSE: []\n")
    inst = Instruments(filtered, cfg, load_holidays(p))
    # expiry moved to Mon 23 Nov (from the master): sessions Thu 19, Fri 20, Mon 23
    assert inst.is_expiry_day("BANKNIFTY", date(2026, 11, 23))
    assert [inst.in_bnf_rollover(date(2026, 11, d)) for d in (18, 19, 20, 23, 24)] == [False, True, True, True, False]


def test_option_tokens(cfg, filtered):
    inst = Instruments(filtered, cfg)
    rows = inst.option_tokens("BANKNIFTY", date(2026, 10, 27), 55020, 1)
    assert sorted({r["strike"] for r in rows}) == [54900, 55000, 55100]
    assert len(rows) == 6 and {r["type"] for r in rows} == {"CE", "PE"}


def _root(tmp_path, data):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "instruments_filtered.json").write_text(json.dumps(data))
    (tmp_path / "holidays.yaml").write_text("NSE: []\nBSE: []\n")
    return tmp_path


def test_ensure_fresh_file_no_subprocess(cfg, filtered, tmp_path):
    root = _root(tmp_path, filtered)
    calls = []
    inst = ensure_instruments(cfg, root, date(2026, 10, 6), runner=lambda *a, **k: calls.append(a))
    assert inst and not calls


def test_ensure_refresh_fails_uses_stale_if_valid(cfg, filtered, tmp_path):
    root = _root(tmp_path, filtered)
    runner = lambda *a, **k: SimpleNamespace(returncode=1, stderr="boom", stdout="")
    inst = ensure_instruments(cfg, root, date(2026, 10, 7), runner=runner)
    assert inst and inst.built == "2026-10-06"
    # NIFTY's last expiry 10-13 < 10-14 -> stale file unusable
    assert ensure_instruments(cfg, root, date(2026, 10, 14), runner=runner) is None


def test_ensure_refresh_success_and_retry(cfg, filtered, tmp_path):
    root = _root(tmp_path, dict(filtered, options={k: [] for k in filtered["options"]}))
    n = {"calls": 0}

    def runner(cmd, **kw):
        n["calls"] += 1
        assert kw["timeout"] == cfg["instruments"]["refresh_timeout_seconds"]
        if n["calls"] < 3:
            return SimpleNamespace(returncode=1, stderr="net down", stdout="")
        (root / "data" / "instruments_filtered.json").write_text(json.dumps(dict(filtered, date="2026-10-07")))
        return SimpleNamespace(returncode=0, stderr="", stdout="ok")

    sleeps = []
    inst = wait_for_instruments(cfg, root, lambda: date(2026, 10, 7), sleep=sleeps.append, runner=runner)
    assert inst.built == "2026-10-07" and sleeps == [120, 120]
