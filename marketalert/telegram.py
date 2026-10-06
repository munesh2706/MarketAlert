"""Telegram: send with retry/backoff through a rate-limited queue; poll commands (getUpdates).

Only TG_CHAT_ID is served. Failures are logged (bot token scrubbed) and never raise.
Commands: /map <index|all>, /mute <index|all> <minutes>, /unmute <index|all>, /status, /help.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

log = logging.getLogger("marketalert")

HELP = ("MarketAlert commands\n"
        "/map <index|all> - market map\n"
        "/mute <index|all> <minutes> - mute alerts\n"
        "/unmute <index|all> - unmute\n"
        "/status - bot health\n"
        "/help - this help\n"
        "Indices: {indices}")


class CommandError(Exception):
    """Invalid command; the message is sent back to the user."""


def parse_command(text: str, indices: list[str]) -> tuple[str, list[Any]]:
    """'/mute nifty 30' -> ('mute', ['NIFTY', 30.0]). Raises CommandError with a user message."""
    parts = text.strip().split()
    if not parts or not parts[0].startswith("/"):
        raise CommandError("Commands start with /. Try /help")
    cmd = parts[0][1:].split("@")[0].lower()
    args = parts[1:]

    def index_arg(a: str | None, default: str | None = None) -> str:
        if a is None:
            if default:
                return default
            raise CommandError(f"Give an index: {', '.join(indices)} or all")
        up = a.upper()
        if up == "ALL":
            return "all"
        if up not in indices:
            raise CommandError(f"Unknown index '{a}'. Use {', '.join(indices)} or all")
        return up

    if cmd == "map":
        return cmd, [index_arg(args[0] if args else None, "all")]
    if cmd == "mute":
        if len(args) != 2:
            raise CommandError("Usage: /mute <index|all> <minutes>")
        try:
            minutes = float(args[1])
        except ValueError:
            raise CommandError("Minutes must be a number, e.g. /mute NIFTY 30") from None
        if not 0 < minutes <= 24 * 60:
            raise CommandError("Minutes must be between 1 and 1440")
        return cmd, [index_arg(args[0]), minutes]
    if cmd == "unmute":
        return cmd, [index_arg(args[0] if args else None)]
    if cmd in ("status", "help", "start"):
        return ("help" if cmd == "start" else cmd), []
    raise CommandError("Unknown command. Try /help")


class TelegramClient:
    """Thin Bot API client (requests). send_now never raises."""

    def __init__(self, token: str, chat_id: str, cfg: dict[str, Any], http: Any = None,
                 sleep: Callable[[float], None] = time.sleep):
        import requests

        self.token, self.chat_id = token, str(chat_id)
        self.t = cfg["telegram"]
        self.http = http or requests
        self.sleep = sleep

    def _url(self, method: str) -> str:
        return f"{self.t['api_base']}/bot{self.token}/{method}"

    def _scrub(self, text: Any) -> str:
        return str(text).replace(self.token, "***")[:300] if self.token else str(text)[:300]

    def send_now(self, text: str) -> bool:
        """Send to TG_CHAT_ID with retries; True on success."""
        text = text[: self.t["max_message_chars"]]
        backoff = self.t["send_backoff_seconds"]
        for attempt in range(self.t["send_retries"] + 1):
            wait = backoff[min(attempt, len(backoff) - 1)]
            try:
                r = self.http.post(self._url("sendMessage"), timeout=self.t["request_timeout_seconds"],
                                   json={"chat_id": self.chat_id, "text": text,
                                         "disable_web_page_preview": True})
                body = r.json() if r.content else {}
                if r.status_code == 200 and body.get("ok"):
                    return True
                if r.status_code == 429:
                    wait = (body.get("parameters") or {}).get("retry_after", wait)
                log.warning("telegram send failed (%s): %s", r.status_code,
                            self._scrub(body.get("description", "")))
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    return False                # bad request / chat: retrying won't help
            except Exception as e:  # noqa: BLE001 - network errors must not crash the bot
                log.warning("telegram send error: %s", self._scrub(f"{type(e).__name__}: {e}"))
            if attempt < self.t["send_retries"]:
                self.sleep(wait)
        return False

    def get_updates(self, offset: int) -> list[dict[str, Any]]:
        r = self.http.get(self._url("getUpdates"), timeout=self.t["request_timeout_seconds"],
                          params={"offset": offset, "timeout": self.t["long_poll_seconds"],
                                  "allowed_updates": '["message"]'})
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(self._scrub(body.get("description", f"HTTP {r.status_code}")))
        return body.get("result", [])


class SendQueue:
    """FIFO of outgoing messages, sent by one thread at <= 1 message per min_interval."""

    def __init__(self, send: Callable[[str], bool], min_interval: float,
                 mono: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.send, self.min_interval, self.mono, self.sleep = send, min_interval, mono, sleep
        self.q: queue.Queue[str] = queue.Queue()
        self._last: float | None = None
        self._stop = threading.Event()
        self.sent = self.failed = 0
        self.thread: threading.Thread | None = None

    def put(self, text: str) -> None:
        self.q.put(text)

    def process_one(self, timeout: float | None = None) -> bool:
        """Send the next message (waiting for the rate limit). False if the queue stayed empty."""
        try:
            text = self.q.get(timeout=timeout)
        except queue.Empty:
            return False
        if self._last is not None:
            wait = self.min_interval - (self.mono() - self._last)
            if wait > 0:
                self.sleep(wait)
        try:
            ok = self.send(text)
        except Exception as e:  # noqa: BLE001
            log.warning("send queue error: %s", type(e).__name__)
            ok = False
        self._last = self.mono()
        self.sent += ok
        self.failed += not ok
        return True

    def drain(self, timeout: float = 30) -> None:
        """Block until the queue is empty or timeout (used before sleeping / exit)."""
        end = time.monotonic() + timeout
        while not self.q.empty() and time.monotonic() < end:
            time.sleep(0.1)

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                self.process_one(timeout=1)
        self.thread = threading.Thread(target=loop, name="tg-send", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()


class CommandPoller:
    """Long-polls getUpdates in a thread; replies only to TG_CHAT_ID; persists the offset."""

    def __init__(self, client: TelegramClient, handler: Callable[[str, list[Any]], str],
                 reply: Callable[[str], None], get_offset: Callable[[], int],
                 set_offset: Callable[[int], None], indices: list[str],
                 sleep: Callable[[float], None] = time.sleep):
        self.client, self.handler, self.reply = client, handler, reply
        self.get_offset, self.set_offset = get_offset, set_offset
        self.indices, self.sleep = indices, sleep
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    def handle_text(self, text: str) -> str:
        try:
            cmd, args = parse_command(text, self.indices)
        except CommandError as e:
            return str(e)
        if cmd == "help":
            return HELP.format(indices=", ".join(self.indices))
        try:
            return self.handler(cmd, args)
        except Exception as e:  # noqa: BLE001
            log.exception("command %s failed", cmd)
            return f"Command failed: {type(e).__name__}"

    def poll_once(self) -> int:
        """One getUpdates round; returns the number of commands answered."""
        n = 0
        for u in self.client.get_updates(self.get_offset()):
            self.set_offset(u["update_id"] + 1)
            msg = u.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = msg.get("text") or ""
            if chat != self.client.chat_id:
                log.warning("ignored message from unauthorised chat %s", chat)
                continue
            if text.startswith("/"):
                self.reply(self.handle_text(text))
                n += 1
        return n

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.poll_once()
                except Exception as e:  # noqa: BLE001
                    log.warning("telegram poll error: %s", self.client._scrub(f"{type(e).__name__}: {e}"))
                    self.sleep(self.client.t["poll_error_backoff_seconds"])
        self.thread = threading.Thread(target=loop, name="tg-poll", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
