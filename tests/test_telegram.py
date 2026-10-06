import pytest

from marketalert.telegram import CommandError, CommandPoller, SendQueue, TelegramClient, parse_command

IDX = ["NIFTY", "BANKNIFTY", "SENSEX"]


def test_parse_commands():
    assert parse_command("/map", IDX) == ("map", ["all"])
    assert parse_command("/map nifty", IDX) == ("map", ["NIFTY"])
    assert parse_command("/map@MyBot SENSEX", IDX) == ("map", ["SENSEX"])
    assert parse_command("/mute all 30", IDX) == ("mute", ["all", 30.0])
    assert parse_command("/unmute banknifty", IDX) == ("unmute", ["BANKNIFTY"])
    assert parse_command("/status", IDX) == ("status", [])
    assert parse_command("/help", IDX) == ("help", [])
    for bad in ("/mute NIFTY", "/mute NIFTY abc", "/mute NIFTY 0", "/map FOO", "/unmute", "/buy", "hello"):
        with pytest.raises(CommandError):
            parse_command(bad, IDX)


class FakeTg:
    chat_id = "42"
    t = {"poll_error_backoff_seconds": 0}

    def __init__(self, updates):
        self.updates = updates

    def get_updates(self, offset):
        return [u for u in self.updates if u["update_id"] >= offset]

    def _scrub(self, s):
        return s


def upd(uid, chat, text):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def test_poller_only_serves_configured_chat():
    replies, offset, calls = [], [0], []
    tg = FakeTg([upd(5, 42, "/status"), upd(6, 999, "/mute all 600"), upd(7, 42, "/map XYZ"),
                 upd(8, 42, "just text")])
    p = CommandPoller(tg, lambda c, a: calls.append((c, a)) or "ok", replies.append,
                      lambda: offset[0], lambda o: offset.__setitem__(0, o), IDX)
    assert p.poll_once() == 2
    assert calls == [("status", [])]                    # chat 999 ignored, bad index not dispatched
    assert replies[0] == "ok" and replies[1].startswith("Unknown index")
    assert offset[0] == 9
    assert p.poll_once() == 0                           # offset persisted -> nothing re-processed


class Resp:
    def __init__(self, code, body):
        self.status_code, self._body, self.content = code, body, b"x"

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, plan):
        self.plan, self.calls = list(plan), []

    def post(self, url, **kw):
        self.calls.append(url)
        r = self.plan.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def tg_client(cfg, plan):
    sleeps = []
    http = FakeHttp(plan)
    return TelegramClient("123:SECRET", "42", cfg, http=http, sleep=sleeps.append), http, sleeps


def test_send_retry_backoff_and_never_raises(cfg, caplog):
    c, http, sleeps = tg_client(cfg, [Resp(500, {"ok": False}), Resp(200, {"ok": True})])
    assert c.send_now("hi") and sleeps == [2]
    c, http, sleeps = tg_client(cfg, [Resp(429, {"ok": False, "parameters": {"retry_after": 7}}),
                                      Resp(200, {"ok": True})])
    assert c.send_now("hi") and sleeps == [7]
    c, http, sleeps = tg_client(cfg, [Resp(400, {"ok": False, "description": "chat not found"})])
    assert not c.send_now("hi") and sleeps == []
    c, http, sleeps = tg_client(cfg, [ConnectionError("https://api.telegram.org/bot123:SECRET/x down")] * 4)
    assert not c.send_now("hi") and sleeps == [2, 5, 10]
    assert "SECRET" not in caplog.text


def test_send_queue_rate_limit():
    sent, sleeps, clock = [], [], [0.0]
    q = SendQueue(lambda t: sent.append(t) or True, 1.0, mono=lambda: clock[0],
                  sleep=lambda s: (sleeps.append(round(s, 2)), clock.__setitem__(0, clock[0] + s)))
    for t in ("a", "b", "c"):
        q.put(t)
    for _ in range(3):
        assert q.process_one(timeout=0)
    assert sent == ["a", "b", "c"] and sleeps == [1.0, 1.0]
    clock[0] += 5                                       # idle gap -> no wait
    q.put("d")
    q.process_one(timeout=0)
    assert sleeps == [1.0, 1.0] and not q.process_one(timeout=0)
