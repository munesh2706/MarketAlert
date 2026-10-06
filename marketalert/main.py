"""Entry point: the full bot (scheduler loop) and the live trading-day session.

Threads: websocket feed (ticks -> engine -> alerts), OI worker (polls -> queue), Telegram
send queue, Telegram command poller, and the main loop (schedule, OI queue, candle clock).
One lock serialises all engine / alert-state access.
"""
from __future__ import annotations

import logging
import logging.handlers
import queue
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from .alerts import Alert, AlertEngine, AlertState, build_map, build_summary, record_map_walls
from .angel import AngelClient, AngelError, LiveFeed, spot_request, ws_token_list
from .config import ROOT, is_market_open, now_ist, today_ist
from .engine import Engine, timeline_line
from .instruments import Instruments, load_holidays, wait_for_instruments
from .recorder import Recorder
from .scheduler import DayRunner, Schedule
from .telegram import CommandPoller, SendQueue, TelegramClient
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


class OiWorker:
    """Polls OI every oi_poll_seconds in its own thread; results go to a thread-safe queue."""

    def __init__(self, fetch: Callable[[], list[tuple]], period: float):
        self.fetch, self.period = fetch, period
        self.q: queue.Queue[tuple] = queue.Queue()
        self._stop = threading.Event()
        self.last_at: datetime | None = None
        self.last_seconds: float | None = None
        self.thread: threading.Thread | None = None

    def poll(self) -> None:
        t0 = time.monotonic()
        try:
            for item in self.fetch():
                self.q.put(item)
        except Exception:  # noqa: BLE001 - a failed poll must not kill the worker
            log.exception("OI poll failed")
        self.last_at, self.last_seconds = now_ist(), round(time.monotonic() - t0, 1)
        log.info("OI poll %.1fs", self.last_seconds)

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                t0 = time.monotonic()
                self.poll()
                self._stop.wait(max(0.0, self.period - (time.monotonic() - t0)))
        self.thread = threading.Thread(target=loop, name="oi", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()


class LiveSession:
    """One trading day: instruments, Angel, engine, feed, OI worker, recorder, alerts."""

    def __init__(self, cfg: dict[str, Any], env: dict[str, str], record: bool = True,
                 alerts: AlertEngine | None = None, notify: Callable[[str], None] | None = None,
                 lock: threading.RLock | None = None):
        self.cfg = cfg
        self.client = AngelClient(cfg, env)
        self.engine = Engine(cfg)
        self.recorder = Recorder(ROOT / cfg["paths"]["recordings_dir"]) if record else None
        self.alerts = alerts
        self.notify = notify or (lambda text: log.info("ALERT %s", text))
        self.lock = lock or threading.RLock()
        self.inst: Instruments | None = None
        self.seed_failed: set[str] = set()
        self.next_reseed: datetime | None = None
        self.feed: LiveFeed | None = None
        self.oi: OiWorker | None = None
        self.started_at: datetime | None = None

    # ------------------------------------------------------------ day actions
    def refresh(self) -> None:
        self.inst = wait_for_instruments(self.cfg, ROOT, today_ist)
        log.info("instruments from %s", self.inst.built if self.inst else None)

    def login(self, retry: bool = True) -> None:
        while True:
            try:
                self.client.login()
                return
            except AngelError as e:
                if not retry:
                    raise
                log.error("%s; retrying in %ss", e, self.cfg["angel"]["login_retry_seconds"])
                time.sleep(self.cfg["angel"]["login_retry_seconds"])

    def start(self, notify_start: bool = True) -> None:
        """Login, EMA seed, websocket, OI worker, health ping."""
        if self.inst is None:
            self.refresh()
        self.login()
        self.seed(list(self.cfg["indices"]))
        tokens = {str(ix["spot_token"]): k for k, ix in self.cfg["indices"].items()}
        token_list = ws_token_list(self.cfg)
        self.feed = LiveFeed(self.cfg, tokens, lambda cb: self.client.make_socket(token_list, cb),
                             lambda: self.client.get_ltp(spot_request(self.cfg)), self.on_tick,
                             relogin=self.client.login)
        self.feed.start()
        self.oi = OiWorker(self.fetch_oi, self.cfg["oi"]["oi_poll_seconds"])
        self.oi.start()
        self.started_at = now_ist()
        if notify_start:
            msg = f"✅ MarketAlert started {self.started_at:%d-%b-%Y}"
            if self.seed_failed:
                msg += f" (EMA pending: {', '.join(sorted(self.seed_failed))})"
            self.notify(msg)

    def has_ticks(self) -> bool:
        return bool(self.feed and self.feed.has_live_data())

    def holiday(self) -> None:
        self.notify("Holiday or no data today")

    def market_map(self) -> None:
        with self.lock:
            if self.alerts:
                record_map_walls(self.engine.states, self.alerts)
            text = build_map(self.engine.states, self.infos(), now_ist())
        self.notify(text)

    def summary(self) -> None:
        with self.lock:
            text = build_summary(self.engine.states, self.alerts, now_ist()) if self.alerts else ""
        if text:
            self.notify(text)

    def stop(self) -> None:
        if self.feed:
            self.feed.stop()
        if self.oi:
            self.oi.stop()
        if self.recorder:
            self.recorder.close()
        if self.feed:
            log.info("session stopped; feed %s", self.feed.stats())

    def tick(self, now: datetime) -> None:
        """Main-loop work: feed health, queued OI snapshots, candle clock, EMA re-seed."""
        if self.feed:
            self.feed.check(now, is_market_open(self.cfg, now))
        if self.oi:
            while True:
                try:
                    item = self.oi.q.get_nowait()
                except queue.Empty:
                    break
                self.apply_oi(*item)
        with self.lock:
            events = self.engine.advance(now)
            self._alert(events, now)
        if self.seed_failed and self.next_reseed and now >= self.next_reseed:
            self.seed(sorted(self.seed_failed))

    # ------------------------------------------------------------ data
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

    def _alert(self, events: list[dict[str, Any]], now: datetime, index: str | None = None) -> None:
        """Run alert rules for indices touched by events (caller holds the lock)."""
        tl = self.cfg["replay"]["timeline_minutes"]
        keys = {index} if index else set()
        for ev in events:
            keys.add(ev["index"])
            if ev["type"] == "candle" and ev["candle"].minutes == tl:
                log.info(timeline_line(self.engine.states[ev["index"]], ev["candle"].end))
            elif ev["type"] == "shift":
                log.info("%s SHIFT %s %g -> %g", ev["index"], ev["side"], ev["old"], ev["new"])
        if not self.alerts:
            return
        for k in keys:
            evs = [e for e in events if e["index"] == k]
            for a in self.alerts.process(self.engine.states[k], evs, now):
                self.send_alert(a)

    def send_alert(self, a: Alert) -> None:
        log.info("ALERT %s %s", a.type, a.key)
        self.notify(a.text)

    def on_tick(self, idx: str, ts: datetime, price: float, source: str = "ws") -> None:
        with self.lock:
            events = self.engine.on_tick(idx, ts, price)
            if self.recorder:
                self.recorder.tick(idx, ts, price)
            self._alert(events, ts, idx)

    def spots(self) -> dict[str, float]:
        """Latest spot per index; REST LTP for any index without ticks yet."""
        out = {k: st.spot for k, st in self.engine.states.items() if st.spot is not None}
        if len(out) < len(self.engine.states):
            ltp = self.client.get_ltp(spot_request(self.cfg))
            for k, ix in self.cfg["indices"].items():
                if k not in out and str(ix["spot_token"]) in ltp:
                    out[k] = ltp[str(ix["spot_token"])]
        return out

    def fetch_oi(self) -> list[tuple]:
        """Network part of an OI poll (no engine access). Returns (index, ts, spot, exps, snap)."""
        assert self.inst
        now = now_ist()
        n = self.cfg["oi"]["strikes_each_side"]
        out: list[tuple] = []
        try:
            spots = self.spots()
        except AngelError as e:
            log.error("spot LTP failed: %s", e)
            return out
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
                out.append((k, now, spot, exps, agg))
            except AngelError as e:
                log.error("%s OI poll failed: %s", k, e)
        return out

    def apply_oi(self, k: str, ts: datetime, spot: float, exps: list, agg: Snapshot) -> None:
        with self.lock:
            events = self.engine.on_oi(k, ts, agg, spot)
            if self.recorder:
                self.recorder.oi(k, ts, spot, exps, agg)
            self._alert(events, ts, k)

    def infos(self) -> dict[str, dict[str, Any]]:
        """Expiry info per index for the market map."""
        out: dict[str, dict[str, Any]] = {}
        if not self.inst:
            return out
        d = today_ist()
        for k in self.cfg["indices"]:
            exps = self.inst.oi_expiries(k, d)
            note = ""
            if len(exps) > 1:
                note = "expiry day: current + next summed" if exps[0] == d else "rollover: current + next summed"
            out[k] = {"expiry": exps[0] if exps else None, "note": note}
        return out

    # ------------------------------------------------------------ dev modes
    def run(self, minutes: float) -> None:
        """Unscheduled session for N minutes (recording / testing). Alerts only to the log."""
        self.start(notify_start=False)
        end = now_ist() + timedelta(minutes=minutes)
        try:
            while now_ist() < end:
                self.tick(now_ist())
                time.sleep(self.cfg["websocket"]["check_seconds"])
        except KeyboardInterrupt:
            log.info("stopped by user")
        finally:
            self.stop()

    def smoke(self) -> dict[str, Any]:
        """Login, instruments, EMA seed (or cache), one OI poll per index. No websocket."""
        t0 = time.monotonic()
        self.refresh()
        self.login(retry=False)
        self.seed(list(self.cfg["indices"]))
        t1 = time.monotonic()
        items = self.fetch_oi()
        poll_s = round(time.monotonic() - t1, 2)
        for item in items:
            self.apply_oi(*item)
        if self.recorder:
            self.recorder.close()
        res = {}
        for k, st in self.engine.states.items():
            w = st.walls
            res[k] = {"spot": st.spot, "wall": w.wall.strike if w and w.wall else None,
                      "floor": w.floor.strike if w and w.floor else None, "pcr": w.pcr if w else None,
                      "emas": {p: (round(v, 2) if v is not None else None) for p, v in st.emas.values().items()}}
        return {"indices": res, "oi_poll_seconds": poll_s, "seed_failed": sorted(self.seed_failed),
                "total_seconds": round(time.monotonic() - t0, 1)}


class Bot:
    """Runs forever: one LiveSession per trading day, Telegram commands, error recovery."""

    def __init__(self, cfg: dict[str, Any], env: dict[str, str], record: bool = True):
        self.cfg, self.env, self.record = cfg, env, record
        self.lock = threading.RLock()
        self.state = AlertState(ROOT / cfg["paths"]["state_file"], today_ist())
        self.alerts = AlertEngine(cfg, self.state)
        self.sched = Schedule(cfg, load_holidays(ROOT / cfg["schedule"]["holidays_file"]))
        self.tg = TelegramClient(env["TG_BOT_TOKEN"], env["TG_CHAT_ID"], cfg)
        self.outbox = SendQueue(self.tg.send_now, cfg["telegram"]["min_send_interval_seconds"])
        self.poller = CommandPoller(self.tg, self.handle_command, self.outbox.put,
                                    lambda: self.state.data.get("tg_offset", 0), self._set_offset,
                                    list(cfg["indices"]))
        self.session: LiveSession | None = None
        self.started_at = now_ist()
        self.next_start: datetime | None = None
        self._last_error: str | None = None

    def _set_offset(self, offset: int) -> None:
        with self.lock:
            self.state.data["tg_offset"] = offset
            self.state.save()

    def notify(self, text: str) -> None:
        log.info("TG> %s", text.splitlines()[0])
        self.outbox.put(text)

    # ------------------------------------------------------------ commands
    def handle_command(self, cmd: str, args: list[Any]) -> str:
        now = now_ist()
        with self.lock:
            if cmd == "map":
                if not self.session or not self.session.started_at:
                    return f"Bot is idle. Next start {self.next_start:%d-%b %H:%M}" if self.next_start \
                        else "Bot is starting; try again shortly."
                keys = list(self.cfg["indices"]) if args[0] == "all" else [args[0]]
                return build_map(self.session.engine.states, self.session.infos(), now, keys)
            if cmd == "mute":
                keys = self.alerts.mute(args[0], args[1], now)
                return f"Muted {', '.join(keys)} for {args[1]:g} min (until {now + timedelta(minutes=args[1]):%H:%M})"
            if cmd == "unmute":
                return f"Unmuted {', '.join(self.alerts.unmute(args[0]))}"
            if cmd == "status":
                return self.status_text(now)
        return "Unknown command. Try /help"

    def status_text(self, now: datetime) -> str:
        up = now - self.started_at
        lines = [f"MarketAlert status {now:%H:%M:%S}",
                 f"Uptime {int(up.total_seconds() // 3600)}h{int(up.total_seconds() % 3600 // 60):02d}m"]
        s = self.session
        if s and s.started_at:
            ages = []
            for k, st in s.engine.states.items():
                ages.append(f"{k} {int((now - st.last_tick).total_seconds())}s" if st.last_tick else f"{k} -")
            lines.append("Last tick: " + ", ".join(ages))
            if s.feed:
                f = s.feed.stats()
                lines.append(f"WS reconnects {f['reconnects']} | REST fallback {'on' if f['fallback_now'] else 'off'}")
            if s.oi and s.oi.last_at:
                lines.append(f"Last OI poll {s.oi.last_at:%H:%M:%S} ({s.oi.last_seconds}s)")
            if s.seed_failed:
                lines.append(f"EMA pending: {', '.join(sorted(s.seed_failed))}")
        else:
            lines.append(f"Idle. Next start {self.next_start:%d-%b %H:%M}" if self.next_start else "Starting")
        lines.append("Alerts today: " + ", ".join(f"{k} {self.alerts.count(k)}" for k in self.cfg["indices"]))
        muted = [f"{k} until {datetime.fromisoformat(v):%H:%M}" for k, v in self.state["mutes"].items()
                 if self.alerts.muted(k, now)]
        lines.append("Muted: " + (", ".join(muted) if muted else "none"))
        return "\n".join(lines)

    # ------------------------------------------------------------ loop
    def sleep_until(self, t: datetime) -> None:
        chunk = self.cfg["schedule"]["sleep_chunk_seconds"]
        while (left := (t - now_ist()).total_seconds()) > 0:
            time.sleep(min(chunk, left))

    def run_day(self) -> str | None:
        self.next_start = self.sched.next_start(now_ist())
        if self.next_start > now_ist():
            log.info("sleeping until %s", f"{self.next_start:%a %d-%b %H:%M}")
            self.sleep_until(self.next_start)
        with self.lock:
            self.state.roll(today_ist())
            self.session = LiveSession(self.cfg, self.env, self.record, self.alerts, self.notify, self.lock)
        runner = DayRunner(self.sched, self.session)
        try:
            while not runner.step(now_ist()):
                time.sleep(self.cfg["schedule"]["loop_seconds"])
        finally:
            self.session.stop()
            self.session = None
        log.info("day finished: %s", runner.result)
        return runner.result

    def run_forever(self) -> None:
        self.outbox.start()
        self.poller.start()
        while True:
            try:
                self.run_day()
                self._last_error = None
            except KeyboardInterrupt:
                raise
            except Exception as e:  # noqa: BLE001 - keep running
                log.exception("unexpected error")
                short = f"{type(e).__name__}: {e}"
                for v in self.env.values():
                    if v and len(v) >= 4:
                        short = short.replace(v, "***")
                short = short[:200]
                if short != self._last_error:
                    self.notify(f"⚠️ MarketAlert error: {short}")
                    self._last_error = short
                time.sleep(self.cfg["schedule"]["error_restart_seconds"])

    def shutdown(self) -> None:
        if self.session:
            self.session.stop()
        self.poller.stop()
        self.outbox.drain(5)
        self.outbox.stop()
