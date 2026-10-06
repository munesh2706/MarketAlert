import json
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from marketalert.angel import AngelClient, AngelError, LiveFeed

from .conftest import ist

ENV = {"ANGEL_API_KEY": "key123", "ANGEL_CLIENT_CODE": "C1", "ANGEL_MPIN": "1111",
       "ANGEL_TOTP_SECRET": "JBSWY3DPEHPK3PXP", "TG_BOT_TOKEN": "", "TG_CHAT_ID": ""}
RL = "Access denied because of exceeding access rate"
OK = {"status": True, "data": {"fetched": []}}


class FakeApi:
    def __init__(self, md=None, candles=None):
        self.md = list(md or [])
        self.candles = candles or []
        self.md_calls, self.hist_calls = [], []
        self.access_token, self.feed_token = "jwt-secret", "feed-secret"

    def generateSession(self, *a):
        return {"status": True, "data": {}}

    def getMarketData(self, mode, toks):
        self.md_calls.append((mode, toks))
        r = self.md.pop(0) if self.md else OK
        if isinstance(r, Exception):
            raise r
        return r

    def getCandleData(self, params):
        self.hist_calls.append(params)
        fr = params["fromdate"]
        return {"status": True, "data": [c for c in self.candles if c[0][:16].replace("T", " ") >= fr]}


def client(cfg, api, root=Path("."), now=None, mono=None):
    sleeps = []
    clock = (lambda: now[0]) if now else (lambda: ist(2026, 10, 7, 12))
    c = AngelClient(cfg, ENV, api_factory=lambda: api, sleep=sleeps.append, clock=clock,
                    mono=mono or (lambda: 0.0), root=root)
    return c, sleeps


def test_rate_limit_backoff_on_quotes(cfg):
    api = FakeApi(md=[Exception(f"Couldn't parse: b'{RL}'"), {"status": False, "message": RL},
                      {"status": True, "data": {"fetched": [{"symbolToken": "1", "ltp": 10.5}]}}])
    c, sleeps = client(cfg, api)
    assert c.get_ltp({"NSE": ["1"]}) == {"1": 10.5}
    assert sleeps == [5, 10]


def test_rate_limit_gives_up_after_retries(cfg):
    api = FakeApi(md=[Exception(RL)] * 10)
    c, sleeps = client(cfg, api)
    with pytest.raises(AngelError, match="rate limited"):
        c.get_ltp({"NSE": ["1"]})
    assert sleeps == [5, 10, 20, 40]


def test_session_error_relogin(cfg):
    api = FakeApi(md=[{"status": False, "errorcode": "AG8001", "message": "Invalid Token"}, OK])
    c, _ = client(cfg, api)
    c.get_ltp({"NSE": ["1"]})
    assert c.logins == 2


def test_errors_never_leak_secrets(cfg):
    api = FakeApi(md=[{"status": False, "errorcode": "X", "message": "bad key123 jwt-secret"}])
    c, _ = client(cfg, api)
    with pytest.raises(AngelError) as e:
        c.get_ltp({"NSE": ["1"]})
    assert "key123" not in str(e.value) and "jwt-secret" not in str(e.value)


def test_get_oi_batches(cfg):
    api = FakeApi()
    c, sleeps = client(cfg, api)
    c.get_oi("NFO", [str(i) for i in range(120)])
    assert [len(t["NFO"]) for _, t in api.md_calls] == [50, 50, 20]
    assert sleeps == [0.2, 0.2]


def make_candles(day, n, start_price=100.0):
    out = []
    t = ist(*day, 9, 15)
    for i in range(n):
        p = start_price + i
        out.append([(t + timedelta(minutes=15 * i)).isoformat(), p, p, p, p, 0])
    return out


def test_history_cache_reuse_and_incremental(cfg, tmp_path):
    candles = make_candles((2026, 10, 6), 25) + make_candles((2026, 10, 7), 25, 200)
    api = FakeApi(candles=candles)
    now = [ist(2026, 10, 7, 10, 10)]                       # 09:15..09:45 complete, 10:00 forming
    mono = iter([0.0, 1.0, 1.0])
    c, sleeps = client(cfg, api, tmp_path, now, mono=lambda: next(mono))
    first = c.get_candles_15m("NIFTY")
    assert len(first) == 25 + 3 and first[-1][1] == 202.0     # forming 10:00 candle dropped
    cache = json.loads((tmp_path / "data" / "history_15m_NIFTY.json").read_text())
    assert cache["date"] == "2026-10-07" and len(cache["candles"]) == 28
    # same-day restart, nothing missing -> no API call
    assert c.get_candles_15m("NIFTY") == first and len(api.hist_calls) == 1
    # 30 min later -> fetch only from the last cached candle
    now[0] = ist(2026, 10, 7, 10, 50)
    later = c.get_candles_15m("NIFTY")
    assert api.hist_calls[-1]["fromdate"] == "2026-10-07 09:45"
    assert len(later) == 31 and later[-1][1] == 205.0        # + 10:00, 10:15, 10:30
    assert sleeps and sleeps[0] == pytest.approx(cfg["history"]["spacing_seconds"] - 1.0)


def test_history_rate_limit_then_ok(cfg, tmp_path):
    class RLApi(FakeApi):
        n = 0

        def getCandleData(self, params):
            RLApi.n += 1
            if RLApi.n <= 2:
                raise Exception(f"Couldn't parse the JSON response: b'{RL}'")
            return super().getCandleData(params)

    api = RLApi(candles=make_candles((2026, 10, 6), 25))
    c, sleeps = client(cfg, api, tmp_path, [ist(2026, 10, 7, 9, 0)])
    assert len(c.get_candles_15m("SENSEX")) == 25
    assert sleeps[:2] == [5, 10]


class FakeSock:
    def __init__(self, on_tick, plan):
        self.on_tick, self.plan = on_tick, plan
        self.closed = threading.Event()

    def connect(self):
        if self.plan == "ticks_then_drop":
            self.on_tick("99926000", 22600.0)
            self.on_tick("99926000", 22601.0)
        elif self.plan == "fail":
            raise ConnectionError("refused")
        elif self.plan == "hang":
            self.closed.wait(5)

    def close(self):
        self.closed.set()


def test_live_feed_drop_reconnect_fallback_resume(cfg):
    plans = iter(["ticks_then_drop", "fail", "fail", "fail", "hang", "hang"])
    socks, ticks, sleeps, relogins = [], [], [], []
    clock = [ist(2026, 10, 7, 10, 0, 0)]

    def factory(cb):
        s = FakeSock(cb, next(plans))
        socks.append(s)
        return s

    rest = lambda: {"99926000": 22650.0, "99919000": 72800.0, "junk": 1.0}
    feed = LiveFeed(cfg, {"99926000": "NIFTY", "99919000": "SENSEX"}, factory, rest,
                    lambda i, t, p, src: ticks.append((i, p, src)), relogin=lambda: relogins.append(1),
                    clock=lambda: clock[0], sleep=sleeps.append)
    feed.start()
    for _ in range(200):
        if len(socks) == 5:
            break
        threading.Event().wait(0.01)
    assert feed.reconnects == 4 and len(socks) == 5
    assert sleeps == [5, 5, 10, 20]                        # reset after ticks, then doubling
    assert relogins == [1]                                 # after 3 tick-less connections
    assert ticks[:2] == [("NIFTY", 22600.0, "ws"), ("NIFTY", 22601.0, "ws")]

    clock[0] += timedelta(seconds=31)                       # silence > 30 s -> REST fallback
    feed.check(clock[0], market_open=False)
    assert feed.fallback and ticks[-2:] == [("NIFTY", 22650.0, "rest"), ("SENSEX", 72800.0, "rest")]
    clock[0] += timedelta(seconds=2)
    feed.check(clock[0], market_open=False)                 # < 5 s since last REST poll
    assert feed.rest_ticks == 2
    clock[0] += timedelta(seconds=5)
    feed.check(clock[0], market_open=False)
    assert feed.rest_ticks == 4

    socks[-1].on_tick("99926000", 22700.0)                  # websocket resumes
    assert not feed.fallback and ticks[-1] == ("NIFTY", 22700.0, "ws")
    feed.stop()
    feed.thread.join(2)
    assert not feed.thread.is_alive()


def test_live_feed_forces_reconnect_when_stale(cfg):
    clock = [ist(2026, 10, 7, 10, 0)]
    socks = []

    def factory(cb):
        s = FakeSock(cb, "hang")
        socks.append(s)
        return s

    feed = LiveFeed(cfg, {"1": "NIFTY"}, factory, lambda: {}, lambda *a: None,
                    clock=lambda: clock[0], sleep=lambda s: None)
    feed.start()
    while not socks:
        threading.Event().wait(0.01)
    clock[0] += timedelta(seconds=61)
    feed.check(clock[0], market_open=True)
    assert socks[0].closed.is_set()
    feed.stop()


def test_login_retries_network_errors(cfg):
    class Flaky(FakeApi):
        n = 0

        def generateSession(self, *a):
            Flaky.n += 1
            if Flaky.n < 3:
                raise TimeoutError("read timed out")
            return {"status": True, "data": {}}

    c, sleeps = client(cfg, Flaky())
    c.login()
    assert c.logins == 1 and sleeps == [2, 4]


def test_sdk_log_redaction():
    import logging

    from marketalert.angel import SecretFilter

    f = SecretFilter()
    f.secrets.add("key123")
    rec = logging.LogRecord("x", logging.ERROR, "", 0, "Headers: %s",
                            ({"Authorization": "Bearer eyJ.a-b", "X-PrivateKey": "key123"},), None)
    f.filter(rec)
    msg = rec.getMessage()
    assert "eyJ" not in msg and "key123" not in msg and "Authorization" in msg
