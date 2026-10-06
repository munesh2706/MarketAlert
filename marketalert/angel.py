"""Angel SmartAPI: TOTP login, quotes/OI, historical candles, websocket, re-login.

- Every REST call goes through AngelClient._call: rate-limit text ("exceeding access rate")
  -> backoff history.backoff_seconds; session errors -> one re-login; network errors -> retry.
- 15-min history is cached per index in data/history_15m_{INDEX}.json; a same-day restart
  fetches only missing candles.
- LiveFeed owns websocket reconnects (unlimited, backoff to 60 s) and falls back to REST LTP
  after 30 s of silence, switching back when websocket ticks resume.
- SmartAPI's own logs go to logs/ only (console handlers removed); tokens are never logged.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .config import IST, ROOT, now_ist
from .instruments import write_json_atomic

log = logging.getLogger("marketalert")
WS_EXCHANGE_TYPE = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4}


class AngelError(Exception):
    """An Angel call failed after retries."""


@contextlib.contextmanager
def in_root():
    """Run with CWD = project root so the SDK's relative logs/ folder lands in ROOT/logs."""
    old = os.getcwd()
    os.chdir(ROOT)
    try:
        yield
    finally:
        os.chdir(old)


class SecretFilter(logging.Filter):
    """Redact credentials and session tokens from SmartAPI log records (it logs request headers)."""

    PATTERNS = [re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+"),
                re.compile(r"('?(?:X-PrivateKey|x-api-key|x-feed-token|Authorization|refreshToken|"
                           r"jwtToken|feedToken|password|totp)'?\s*[:=]\s*'?)[^',}\s]+", re.I)]

    def __init__(self) -> None:
        super().__init__()
        self.secrets: set[str] = set()

    def redact(self, text: str) -> str:
        for v in self.secrets:
            text = text.replace(v, "***")
        for p in self.PATTERNS:
            text = p.sub(lambda m: m.group(1) + "***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        return True


SECRET_FILTER = SecretFilter()


def quiet_sdk_console(secrets: list[Any] | None = None) -> None:
    """SmartAPI logs go to logs/ files only (console handlers removed), with secrets redacted."""
    import logzero

    SECRET_FILTER.secrets.update(str(s) for s in (secrets or []) if s and len(str(s)) >= 4)
    for h in list(logzero.logger.handlers):
        if not isinstance(h, logging.FileHandler):
            logzero.logger.removeHandler(h)
    if SECRET_FILTER not in logzero.logger.filters:
        logzero.logger.addFilter(SECRET_FILTER)


class AngelClient:
    """REST client with throttling, rate-limit backoff and automatic re-login."""

    def __init__(self, cfg: dict[str, Any], env: dict[str, str], api_factory: Callable | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], datetime] = now_ist,
                 mono: Callable[[], float] = time.monotonic, root: Path = ROOT):
        self.cfg, self.env = cfg, env
        self.api_factory = api_factory
        self.sleep, self.clock, self.mono, self.root = sleep, clock, mono, root
        self.api: Any = None
        self.jwt = self.feed = None
        self.logins = 0
        self._last_hist: float | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ session
    def login(self) -> None:
        """TOTP login with network retries; raises AngelError on failure."""
        import pyotp

        a = self.cfg["angel"]
        for attempt in range(a["network_retries"] + 1):
            if self.api_factory:
                api = self.api_factory()
            else:
                from SmartApi import SmartConnect

                quiet_sdk_console(list(self.env.values()))
                with in_root():
                    api = SmartConnect(api_key=self.env["ANGEL_API_KEY"],
                                       timeout=self.cfg["network"]["timeout_seconds"])
                quiet_sdk_console()
            try:
                totp = pyotp.TOTP(self.env["ANGEL_TOTP_SECRET"]).now()
                resp = api.generateSession(self.env["ANGEL_CLIENT_CODE"], self.env["ANGEL_MPIN"], totp)
            except Exception as e:  # noqa: BLE001 - timeouts / connection errors
                if attempt < a["network_retries"]:
                    log.warning("login network error (%s), retry %d", type(e).__name__, attempt + 1)
                    self.sleep(a["network_backoff_seconds"] * (attempt + 1))
                    continue
                raise AngelError(f"login failed: {type(e).__name__}: {self._scrub(e)}") from None
            break
        if not resp or not resp.get("status"):
            raise AngelError(f"login failed: {self._scrub(self._msg(resp))}")
        quiet_sdk_console([getattr(api, "access_token", None), getattr(api, "feed_token", None),
                           getattr(api, "refresh_token", None)])
        self.api = api
        self.jwt = getattr(api, "access_token", None)
        self.feed = getattr(api, "feed_token", None)
        self.logins += 1
        log.info("angel login ok (#%d)", self.logins)

    def _scrub(self, text: Any) -> str:
        s = str(text)
        for v in [*self.env.values(), self.jwt, self.feed]:
            if v and len(str(v)) >= 4:
                s = s.replace(str(v), "***")
        return s[:300]

    @staticmethod
    def _msg(resp: Any) -> str:
        if not resp:
            return "empty response"
        return f"{resp.get('errorcode', '')} {resp.get('message', '')}".strip()

    def _rate_limited(self, text: str) -> bool:
        return self.cfg["angel"]["rate_limit_text"].lower() in text.lower()

    def _session_error(self, text: str) -> bool:
        a = self.cfg["angel"]
        t = text.lower()
        return any(c.lower() in t for c in a["session_error_codes"]) or \
            any(s.lower() in t for s in a["session_error_text"])

    def _call(self, what: str, fn: Callable[[Any], Any]) -> dict[str, Any]:
        """Run fn(api) with rate-limit backoff, one re-login on session errors, network retries."""
        h, a = self.cfg["history"], self.cfg["angel"]
        backoff = h["backoff_seconds"]
        rl = net = 0
        relogged = False
        while True:
            if self.api is None:
                self.login()
            try:
                resp = fn(self.api)
                text = "" if resp and resp.get("status") else self._msg(resp)
            except Exception as e:  # noqa: BLE001 - SDK raises on non-JSON replies (rate limit)
                resp, text = None, f"{type(e).__name__}: {e}"
                if not self._rate_limited(text) and not self._session_error(text):
                    if net < a["network_retries"]:
                        net += 1
                        log.warning("%s network error, retry %d: %s", what, net, self._scrub(text))
                        self.sleep(a["network_backoff_seconds"] * net)
                        continue
                    raise AngelError(f"{what}: {self._scrub(text)}") from None
            if not text:
                return resp
            if self._rate_limited(text):
                if rl >= h["retries"]:
                    raise AngelError(f"{what}: rate limited after {rl} retries")
                wait = backoff[min(rl, len(backoff) - 1)]
                rl += 1
                log.warning("%s rate limited; backoff %ss (retry %d/%d)", what, wait, rl, h["retries"])
                self.sleep(wait)
                continue
            if self._session_error(text) and not relogged:
                relogged = True
                log.warning("%s session error; re-login", what)
                self.api = None
                continue
            raise AngelError(f"{what}: {self._scrub(text)}")

    # ------------------------------------------------------------ quotes
    def get_ltp(self, tokens: dict[str, list[str]]) -> dict[str, float]:
        """REST LTP for {exchange: [tokens]} -> {token: ltp}."""
        resp = self._call("ltp", lambda api: api.getMarketData("LTP", tokens))
        return {f["symbolToken"]: float(f["ltp"]) for f in (resp.get("data") or {}).get("fetched", [])}

    def get_oi(self, exchange: str, tokens: list[str]) -> dict[str, float | None]:
        """OI for option tokens, batches <= quote_batch_size -> {token: oi}."""
        bs = min(self.cfg["oi"]["quote_batch_size"], 50)
        out: dict[str, float | None] = {}
        for i in range(0, len(tokens), bs):
            if i:
                self.sleep(self.cfg["angel"]["quote_spacing_seconds"])
            batch = tokens[i:i + bs]
            resp = self._call("oi", lambda api, b=batch: api.getMarketData("FULL", {exchange: b}))
            for f in (resp.get("data") or {}).get("fetched", []):
                out[f["symbolToken"]] = f.get("opnInterest")
        return out

    # ------------------------------------------------------------ history
    def _hist_wait(self) -> None:
        gap = self.cfg["history"]["spacing_seconds"]
        if self._last_hist is not None:
            wait = gap - (self.mono() - self._last_hist)
            if wait > 0:
                self.sleep(wait)

    def _cache_path(self, index: str) -> Path:
        return self.root / self.cfg["history"]["cache_file"].format(index=index)

    def get_candles_15m(self, index: str, now: datetime | None = None) -> list[tuple[datetime, float]]:
        """Completed 15-min spot candles (start, close) for the last seed_trading_days, cached."""
        now = (now or self.clock()).astimezone(IST)
        h = self.cfg["history"]
        ix = self.cfg["indices"][index]
        tf = timedelta(minutes=self.cfg["candles"]["ema_timeframe_minutes"])
        path = self._cache_path(index)
        candles: dict[datetime, list[float]] = {}
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("date") == now.date().isoformat():
                candles = {datetime.fromisoformat(r[0]): r[1:] for r in cached.get("candles", [])}
        except (OSError, ValueError):
            pass
        if candles:
            last = max(candles)
            if last + 2 * tf > now:           # no completed candle missing
                log.info("%s history: cache hit (%d candles)", index, len(candles))
                return [(t, v[3]) for t, v in sorted(candles.items())]
            start = last
        else:
            start = now - timedelta(days=h["calendar_days"])
        params = {"exchange": ix["spot_exchange"], "symboltoken": str(ix["spot_token"]),
                  "interval": "FIFTEEN_MINUTE", "fromdate": start.strftime("%Y-%m-%d %H:%M"),
                  "todate": now.strftime("%Y-%m-%d %H:%M")}
        with self._lock:
            self._hist_wait()
            try:
                resp = self._call(f"history {index}", lambda api: api.getCandleData(params))
            finally:
                self._last_hist = self.mono()
        fetched = 0
        for r in resp.get("data") or []:
            t = datetime.fromisoformat(r[0]).astimezone(IST)
            if t + tf <= now:                  # drop the forming candle
                candles[t] = [float(x) for x in r[1:5]]
                fetched += 1
        days = sorted({t.date() for t in candles})[-self.cfg["ema"]["seed_trading_days"]:]
        keep = sorted((t, v) for t, v in candles.items() if t.date() in set(days))
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, {"date": now.date().isoformat(), "index": index,
                                 "candles": [[t.isoformat(), *v] for t, v in keep]})
        log.info("%s history: fetched %d, total %d candles over %d days", index, fetched, len(keep), len(days))
        return [(t, v[3]) for t, v in keep]

    # ------------------------------------------------------------ websocket
    def make_socket(self, token_list: list[dict[str, Any]], on_tick: Callable[[str, float], None]) -> "AngelSocket":
        return AngelSocket(self, token_list, on_tick)


class AngelSocket:
    """One SmartWebSocketV2 connection (SDK retries disabled). connect() blocks until closed."""

    def __init__(self, client: AngelClient, token_list: list[dict[str, Any]],
                 on_tick: Callable[[str, float], None]):
        self.client, self.token_list, self.on_tick = client, token_list, on_tick
        self.ws: Any = None

    def connect(self) -> None:
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2

        c, w = self.client, self.client.cfg["websocket"]
        if c.api is None:
            c.login()
        with in_root():
            ws = SmartWebSocketV2(c.jwt, c.env["ANGEL_API_KEY"], c.env["ANGEL_CLIENT_CODE"], c.feed,
                                  max_retry_attempt=w["sdk_max_retry_attempts"],
                                  retry_delay=w["sdk_retry_delay_seconds"])
        quiet_sdk_console([c.jwt, c.feed])

        def on_data(_wsapp: Any, msg: Any) -> None:
            if isinstance(msg, dict) and "last_traded_price" in msg:
                self.on_tick(str(msg.get("token", "")).strip("\x00"), msg["last_traded_price"] / 100)

        ws.on_open = lambda _wsapp: ws.subscribe("ma000001", 1, self.token_list)
        ws.on_data = on_data
        ws.on_error = lambda *a: None
        self.ws = ws
        ws.connect()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            if self.ws:
                self.ws.close_connection()


class LiveFeed:
    """Spot ticks from the websocket with our own reconnect loop and REST LTP fallback."""

    def __init__(self, cfg: dict[str, Any], tokens: dict[str, str],
                 socket_factory: Callable[[Callable[[str, float], None]], Any],
                 rest_ltp: Callable[[], dict[str, float]],
                 on_tick: Callable[[str, datetime, float, str], None],
                 relogin: Callable[[], None] | None = None,
                 clock: Callable[[], datetime] = now_ist, sleep: Callable[[float], None] | None = None):
        self.cfg = cfg
        self.tokens = tokens                    # token -> index key
        self.factory, self.rest_ltp, self.on_tick = socket_factory, rest_ltp, on_tick
        self.relogin, self.clock = relogin, clock
        self._stop = threading.Event()
        self.sleep = sleep or (lambda s: self._stop.wait(s))
        self.sock: Any = None
        self.thread: threading.Thread | None = None
        self.started: datetime | None = None
        self.last_ws_tick: datetime | None = None
        self.last_rest: datetime | None = None
        self.last_force: datetime | None = None
        self.fallback = False
        self.reconnects = self.fallbacks = self.ws_ticks = self.rest_ticks = 0
        self.fail_streak = 0

    def start(self) -> None:
        self.started = self.clock()
        self.thread = threading.Thread(target=self.run_ws_loop, name="ws", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self.sock:
            self.sock.close()

    def _on_ws_tick(self, token: str, price: float) -> None:
        idx = self.tokens.get(token)
        if idx is None or price <= 0:
            return
        now = self.clock()
        self.last_ws_tick = now
        self.ws_ticks += 1
        if self.fallback:
            self.fallback = False
            log.info("websocket ticks resumed; REST fallback off")
        self.on_tick(idx, now, price, "ws")

    def run_ws_loop(self) -> None:
        """Connect, block until the socket closes, reconnect with backoff. Unlimited attempts."""
        w = self.cfg["websocket"]
        delay = w["reconnect_backoff_seconds"]
        while not self._stop.is_set():
            before = self.ws_ticks
            try:
                self.sock = self.factory(self._on_ws_tick)
                self.sock.connect()
            except Exception as e:  # noqa: BLE001
                log.warning("websocket error: %s", type(e).__name__)
            if self._stop.is_set():
                break
            self.reconnects += 1
            got = self.ws_ticks > before
            if got:
                delay, self.fail_streak = w["reconnect_backoff_seconds"], 0
            else:
                self.fail_streak += 1
                if self.relogin and self.fail_streak % w["relogin_after_failures"] == 0:
                    try:
                        self.relogin()
                    except Exception as e:  # noqa: BLE001
                        log.warning("re-login failed: %s", type(e).__name__)
            log.warning("websocket closed; reconnect #%d in %ss", self.reconnects, delay)
            self.sleep(delay)
            if not got:
                delay = min(delay * 2, w["reconnect_backoff_max_seconds"])

    def check(self, now: datetime, market_open: bool = True) -> None:
        """Call every ~1 s: REST fallback after silence, force reconnect on a stale socket."""
        d, w = self.cfg["data"], self.cfg["websocket"]
        ref = self.last_ws_tick or self.started or now
        silent = (now - ref).total_seconds()
        if not self.fallback and silent > d["ws_fallback_after_seconds"]:
            self.fallback = True
            self.fallbacks += 1
            log.warning("no websocket ticks for %.0fs; REST LTP fallback on", silent)
        if self.fallback and (self.last_rest is None or
                              (now - self.last_rest).total_seconds() >= d["rest_ltp_poll_seconds"]):
            self.last_rest = now
            try:
                for tok, price in self.rest_ltp().items():
                    if tok in self.tokens:
                        self.rest_ticks += 1
                        self.on_tick(self.tokens[tok], now, price, "rest")
            except Exception as e:  # noqa: BLE001
                log.warning("REST LTP fallback failed: %s", e)
        stale = w["stale_reconnect_seconds"]
        if market_open and silent > stale and self.sock and \
                (self.last_force is None or (now - self.last_force).total_seconds() > stale):
            self.last_force = now
            log.warning("websocket silent %.0fs; forcing reconnect", silent)
            self.sock.close()

    def stats(self) -> dict[str, Any]:
        return {"reconnects": self.reconnects, "fallbacks": self.fallbacks, "fallback_now": self.fallback,
                "ws_ticks": self.ws_ticks, "rest_ticks": self.rest_ticks}


def ws_token_list(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Websocket subscription list for the configured spot tokens."""
    by_type: dict[int, list[str]] = {}
    for ix in cfg["indices"].values():
        by_type.setdefault(WS_EXCHANGE_TYPE[ix["spot_exchange"]], []).append(str(ix["spot_token"]))
    return [{"exchangeType": t, "tokens": toks} for t, toks in by_type.items()]


def spot_request(cfg: dict[str, Any]) -> dict[str, list[str]]:
    """{exchange: [spot tokens]} for REST LTP."""
    req: dict[str, list[str]] = {}
    for ix in cfg["indices"].values():
        req.setdefault(ix["spot_exchange"], []).append(str(ix["spot_token"]))
    return req

