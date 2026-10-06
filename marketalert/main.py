"""Entry point: live session (M1: engine + recording, no alerts/Telegram yet)."""
from __future__ import annotations

import logging
import logging.handlers
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from .angel import AngelClient, AngelError, LiveFeed, spot_request, ws_token_list
from .config import ROOT, is_market_open, now_ist, today_ist
from .engine import Engine, timeline_line
from .instruments import Instruments, wait_for_instruments
from .recorder import Recorder
from .walls import Snapshot, sum_snapshots

log = logging.getLogger("marketalert")


def setup_logging(cfg: dict[str, Any]) -> None:
    """App log to logs/marketalert.log (rotating) and the console."""
    lc = cfg["logging"]
    path = ROOT / lc["file"]
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    log.handlers.clear()
    fh = logging.handlers.RotatingFileHandler(path, maxBytes=lc["max_bytes"], backupCount=lc["backups"],
                                              encoding="utf-8")
    sh = logging.StreamHandler(sys.stdout)
    for h in (fh, sh):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(lc["level"])
    log.propagate = False


class LiveSession:
    """Wires instruments, Angel client, engine, websocket feed and recorder."""

    def __init__(self, cfg: dict[str, Any], env: dict[str, str], record: bool = True):
        self.cfg = cfg
        self.client = AngelClient(cfg, env)
        self.engine = Engine(cfg)
        self.recorder = Recorder(ROOT / cfg["paths"]["recordings_dir"]) if record else None
        self.inst: Instruments | None = None
        self.lock = threading.Lock()
        self.seed_failed: set[str] = set()
        self.next_reseed: datetime | None = None
        self.feed: LiveFeed | None = None

    # ------------------------------------------------------------ setup
    def setup(self, retry_login: bool = True) -> None:
        self.inst = wait_for_instruments(self.cfg, ROOT, today_ist)
        log.info("instruments from %s", self.inst.built if self.inst else None)
        while True:
            try:
                self.client.login()
                break
            except AngelError as e:
                if not retry_login:
                    raise
                log.error("%s; retrying in %ss", e, self.cfg["angel"]["login_retry_seconds"])
                time.sleep(self.cfg["angel"]["login_retry_seconds"])
        self.seed(list(self.cfg["indices"]))

    def seed(self, keys: list[str]) -> None:
        """Seed EMAs sequentially; failures start without EMA and are retried later."""
        for k in keys:
            try:
                candles = self.client.get_candles_15m(k)
                with self.lock:
                    ok = self.engine.states[k].seed_emas(candles)
                if self.recorder:
                    self.recorder.seed(k, today_ist(), candles)
            except AngelError as e:
                ok = False
                log.error("%s EMA seed failed: %s", k, e)
            if ok:
                self.seed_failed.discard(k)
                vals = self.engine.states[k].emas.values()
                log.info("%s EMA seeded: %s", k, {p: round(v, 2) for p, v in vals.items()})
            else:
                self.seed_failed.add(k)
        if self.seed_failed:
            self.next_reseed = now_ist() + timedelta(minutes=self.cfg["history"]["reseed_minutes"])
            log.warning("EMA unavailable for %s; re-seed at %s", sorted(self.seed_failed),
                        f"{self.next_reseed:%H:%M}")

    # ------------------------------------------------------------ data
    def on_tick(self, idx: str, ts: datetime, price: float, source: str = "ws") -> None:
        with self.lock:
            events = self.engine.on_tick(idx, ts, price)
            if self.recorder:
                self.recorder.tick(idx, ts, price)
        self.handle(events)

    def handle(self, events: list[dict[str, Any]]) -> None:
        tl = self.cfg["replay"]["timeline_minutes"]
        for ev in events:
            if ev["type"] == "shift":
                log.info("%s SHIFT %s %g -> %g", ev["index"], ev["side"], ev["old"], ev["new"])
            elif ev["type"] == "candle" and ev["candle"].minutes == tl:
                log.info(timeline_line(self.engine.states[ev["index"]], ev["candle"].end))

    def spots(self) -> dict[str, float]:
        """Latest spot per index; REST LTP for any index without ticks yet."""
        out = {k: st.spot for k, st in self.engine.states.items() if st.spot is not None}
        if len(out) < len(self.engine.states):
            ltp = self.client.get_ltp(spot_request(self.cfg))
            for k, ix in self.cfg["indices"].items():
                if k not in out and str(ix["spot_token"]) in ltp:
                    out[k] = ltp[str(ix["spot_token"])]
        return out

    def poll_oi(self) -> dict[str, Any]:
        """One OI poll of every index (current expiry, plus next when summing applies)."""
        assert self.inst
        t0 = time.monotonic()
        now = now_ist()
        n = self.cfg["oi"]["strikes_each_side"]
        result: dict[str, Any] = {}
        try:
            spots = self.spots()
        except AngelError as e:
            log.error("spot LTP failed: %s", e)
            return {}
        for k, ix in self.cfg["indices"].items():
            spot = spots.get(k)
            if spot is None:
                continue
            try:
                exps = self.inst.oi_expiries(k, now.date())
                snaps: list[Snapshot] = []
                for e in exps:
                    rows = self.inst.option_tokens(k, e, spot, n)
                    oi = self.client.get_oi(ix["option_exchange"], [r["token"] for r in rows])
                    snap: Snapshot = {}
                    for r in rows:
                        snap.setdefault(r["strike"], {"CE": None, "PE": None})[r["type"]] = oi.get(r["token"])
                    snaps.append(snap)
                agg = sum_snapshots(snaps) if len(snaps) > 1 else (snaps[0] if snaps else {})
                with self.lock:
                    events = self.engine.on_oi(k, now, agg, spot)
                    if self.recorder:
                        self.recorder.oi(k, now, spot, exps, agg)
                self.handle(events)
                w = self.engine.states[k].walls
                result[k] = {"spot": spot, "expiries": [e.isoformat() for e in exps], "strikes": len(agg),
                             "oi_nonnull": sum(v["CE"] is not None for v in agg.values()) +
                             sum(v["PE"] is not None for v in agg.values()),
                             "wall": w.wall.strike if w and w.wall else None,
                             "floor": w.floor.strike if w and w.floor else None, "pcr": w.pcr if w else None}
                log.info(timeline_line(self.engine.states[k], now))
            except AngelError as e:
                log.error("%s OI poll failed: %s", k, e)
        result["_seconds"] = round(time.monotonic() - t0, 2)
        return result

    # ------------------------------------------------------------ run
    def run(self, minutes: float) -> None:
        self.setup()
        tokens = {str(ix["spot_token"]): k for k, ix in self.cfg["indices"].items()}
        token_list = ws_token_list(self.cfg)
        self.feed = LiveFeed(self.cfg, tokens, lambda cb: self.client.make_socket(token_list, cb),
                             lambda: self.client.get_ltp(spot_request(self.cfg)), self.on_tick,
                             relogin=self.client.login)
        self.feed.start()
        end = now_ist() + timedelta(minutes=minutes)
        next_oi = now_ist()
        try:
            while now_ist() < end:
                now = now_ist()
                self.feed.check(now, is_market_open(self.cfg, now))
                with self.lock:
                    events = self.engine.advance(now)
                self.handle(events)
                if now >= next_oi:
                    r = self.poll_oi()
                    log.info("OI poll %.1fs; feed %s", r.get("_seconds", 0), self.feed.stats())
                    next_oi = now + timedelta(seconds=self.cfg["oi"]["oi_poll_seconds"])
                if self.seed_failed and self.next_reseed and now >= self.next_reseed:
                    self.seed(sorted(self.seed_failed))
                time.sleep(self.cfg["websocket"]["check_seconds"])
        except KeyboardInterrupt:
            log.info("stopped by user")
        finally:
            self.feed.stop()
            if self.recorder:
                self.recorder.close()
            log.info("session end; feed %s", self.feed.stats())

    def smoke(self) -> dict[str, Any]:
        """Login, instruments, EMA seed (or cache), one OI poll per index. No websocket."""
        t0 = time.monotonic()
        self.setup(retry_login=False)
        r = self.poll_oi()
        if self.recorder:
            self.recorder.close()
        emas = {k: {p: (round(v, 2) if v is not None else None) for p, v in st.emas.values().items()}
                for k, st in self.engine.states.items()}
        return {"oi": r, "emas": emas, "seed_failed": sorted(self.seed_failed),
                "total_seconds": round(time.monotonic() - t0, 1)}
