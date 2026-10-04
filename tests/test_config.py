from datetime import datetime, timedelta, timezone

from marketalert import config as c

TABLE = {  # Section 5: step, zone, reset gap, breakout buffer, option exchange
    "NIFTY": (50, 10, 30, 5, "NFO"),
    "BANKNIFTY": (100, 25, 75, 15, "NFO"),
    "SENSEX": (100, 25, 75, 15, "BFO"),
}


def test_indices_match_spec():
    cfg = c.load_config()
    assert set(cfg["indices"]) == set(TABLE)
    for key, (step, zone, gap, buf, exch) in TABLE.items():
        ix = cfg["indices"][key]
        assert (ix["strike_step"], ix["zone"], ix["reset_gap"], ix["breakout_buffer"]) == (step, zone, gap, buf)
        assert ix["option_exchange"] == exch


def test_core_values():
    cfg = c.load_config()
    assert cfg["oi"]["oi_poll_seconds"] == 180
    assert cfg["oi"]["strikes_each_side"] == 15
    assert cfg["oi"]["quote_batch_size"] <= 50
    assert cfg["oi"]["oi_change_pct"] == 10
    assert cfg["alerts"]["max_alerts_per_index"] == 40
    assert cfg["ema"]["periods"] == [50, 200]


def test_times_are_strings_and_parse():
    cfg = c.load_config()
    for section in ("schedule", "alerts"):
        for k, v in cfg[section].items():
            if isinstance(v, str) and ":" in v:
                c.parse_hhmm(v)
    assert c.parse_hhmm(cfg["alerts"]["window_start"]).hour == 9
    assert c.parse_hhmm(cfg["alerts"]["window_end"]).minute == 45
    assert cfg["schedule"]["summary"] == "15:45"


def test_ist_helpers():
    n = c.now_ist()
    assert n.utcoffset() == timedelta(hours=5, minutes=30)
    utc = datetime.now(timezone.utc)
    assert abs((n - utc).total_seconds()) < 5
    assert c.today_ist() == n.date()


def test_market_open():
    cfg = c.load_config()
    mon_10 = datetime(2026, 10, 5, 10, 0, tzinfo=c.IST)
    sun_10 = datetime(2026, 10, 4, 10, 0, tzinfo=c.IST)
    mon_utc_0430 = datetime(2026, 10, 5, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
    assert c.is_market_open(cfg, mon_10)
    assert not c.is_market_open(cfg, sun_10)
    assert c.is_market_open(cfg, mon_utc_0430)
    assert not c.is_market_open(cfg, datetime(2026, 10, 5, 16, 0, tzinfo=c.IST))


def test_load_env_missing_file(tmp_path, monkeypatch):
    for k in c.ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    env = c.load_env(tmp_path / "nope.env")
    assert set(env) == set(c.ENV_KEYS)
    assert c.missing_env(env) == list(c.ENV_KEYS)


def test_load_env_file(tmp_path, monkeypatch):
    for k in c.ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    p = tmp_path / ".env"
    p.write_text("ANGEL_API_KEY=abc\nTG_CHAT_ID=123\n")
    env = c.load_env(p)
    assert env["ANGEL_API_KEY"] == "abc"
    assert "ANGEL_API_KEY" not in c.missing_env(env)
