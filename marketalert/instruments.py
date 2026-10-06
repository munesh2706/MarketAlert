"""Scrip master download/cache, index tokens, option tokens, expiry selection.

Low-memory design: the ~35 MB master is streamed to disk and parsed object by object,
keeping only option rows of the configured indices in data/instruments_filtered.json.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Any, Iterator

MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}


def parse_expiry(s: str) -> date | None:
    """Parse master expiry like '07OCT2026' into a date (locale independent)."""
    try:
        return date(int(s[5:9]), MONTHS[s[2:5].upper()], int(s[0:2]))
    except (KeyError, ValueError, IndexError):
        return None


def option_type(row: dict[str, Any]) -> str | None:
    """Return 'CE'/'PE' for an option row, else None.

    The master has no option-type field (instrumenttype is 'OPTIDX'), so the trailing two
    characters of the symbol are the only source. Nothing else is ever parsed from symbols
    (formats differ, e.g. SENSEX weekly 'SENSEX26O0872500PE'): strike and expiry come from
    the master's strike/expiry fields.
    """
    suffix = str(row.get("symbol", ""))[-2:]
    return suffix if suffix in ("CE", "PE") else None


def write_json_atomic(path: Path, obj: Any) -> None:
    """Write JSON to a temp file then rename over the target."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def download_master(url: str, dest: Path, timeout: float, chunk_bytes: int) -> dict[str, float]:
    """Stream the scrip master to dest in chunks; return seconds and size in MB."""
    import requests

    t = time.monotonic()
    tmp = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=(15, timeout)) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_content(chunk_size=chunk_bytes):
                f.write(chunk)
    os.replace(tmp, dest)
    return {"seconds": round(time.monotonic() - t, 1), "mb": round(dest.stat().st_size / 1e6, 1)}


def iter_master_rows(path: Path, chunk_chars: int = 1 << 20) -> Iterator[dict[str, Any]]:
    """Yield each object of the master's top-level JSON array without loading the whole file."""
    dec = json.JSONDecoder()
    buf, pos, eof = "", 0, False
    with path.open(encoding="utf-8") as f:
        while True:
            i = buf.find("{", pos)
            if i == -1:
                if eof:
                    return
                buf, pos = f.read(chunk_chars), 0
                eof = not buf
                continue
            try:
                obj, end = dec.raw_decode(buf, i)
            except json.JSONDecodeError:
                if eof:
                    raise
                chunk = f.read(chunk_chars)
                eof = not chunk
                buf, pos = buf[i:] + chunk, 0
                continue
            yield obj
            pos = end


def build_filtered(master: Path, indices: dict[str, Any], strike_divisor: float,
                   today: date, chunk_chars: int = 1 << 20) -> dict[str, Any]:
    """Parse the master and keep spot rows + unexpired option rows of the configured indices."""
    want = {(v["option_exchange"], v["option_name"]): k for k, v in indices.items()}
    spot_want = {(v["spot_exchange"], str(v["spot_token"])): k for k, v in indices.items()}
    spot: dict[str, dict[str, str]] = {}
    options: dict[str, list[dict[str, Any]]] = {k: [] for k in indices}
    rows = skipped = 0
    for row in iter_master_rows(master, chunk_chars):
        rows += 1
        seg = row.get("exch_seg")
        if row.get("instrumenttype") == "OPTIDX":
            k = want.get((seg, row.get("name")))
            if k:
                exp = parse_expiry(row.get("expiry", ""))
                otype = option_type(row)
                if not exp or otype is None:
                    skipped += 1
                elif exp >= today:
                    options[k].append({"token": row["token"], "symbol": row["symbol"],
                                       "expiry": exp.isoformat(),
                                       "strike": float(row["strike"]) / strike_divisor,
                                       "type": otype})
        elif (seg, row.get("token")) in spot_want:
            spot[spot_want[(seg, row["token"])]] = {"token": row["token"], "symbol": row.get("symbol")}
    return {"date": today.isoformat(), "master_rows": rows, "skipped_option_rows": skipped,
            "spot": spot, "options": options}


def load_filtered(path: Path, today: date) -> dict[str, Any] | None:
    """Return the filtered instruments if the file exists and was built today, else None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if data.get("date") == today.isoformat() else None


def expiries(options: list[dict[str, Any]]) -> list[str]:
    """Sorted unique ISO expiry dates."""
    return sorted({o["expiry"] for o in options})


def select_atm_options(options: list[dict[str, Any]], spot: float, step: float,
                       each_side: int) -> dict[str, Any]:
    """Pick nearest-expiry CE/PE rows for ATM +/- each_side listed strikes."""
    exps = expiries(options)
    if not exps:
        return {"rows": [], "strikes": [], "atm": None, "expiry": None}
    near = [o for o in options if o["expiry"] == exps[0]]
    strikes = sorted({o["strike"] for o in near})
    atm = round(spot / step) * step
    if atm not in strikes:
        atm = min(strikes, key=lambda s: abs(s - spot))
    i = strikes.index(atm)
    sel = strikes[max(0, i - each_side): i + each_side + 1]
    chosen = set(sel)
    return {"rows": [o for o in near if o["strike"] in chosen], "strikes": sel,
            "atm": atm, "expiry": exps[0]}


# ---------------------------------------------------------------- bot side
def load_holidays(path: Path) -> dict[str, set[date]]:
    """Load holidays.yaml -> {exchange: {dates}} (accepts YAML dates or 'YYYY-MM-DD' strings)."""
    import yaml

    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except OSError:
        return {}
    return {str(ex): {d if isinstance(d, date) else date.fromisoformat(str(d)) for d in (days or [])}
            for ex, days in raw.items()}


def is_trading_day(d: date, holidays: set[date]) -> bool:
    """Weekday and not a listed holiday."""
    return d.weekday() < 5 and d not in holidays


class Instruments:
    """Bot-side view of data/instruments_filtered.json (never touches the full master)."""

    def __init__(self, data: dict[str, Any], cfg: dict[str, Any],
                 holidays: dict[str, set[date]] | None = None):
        self.data = data
        self.cfg = cfg
        self.holidays = holidays or {}

    @property
    def built(self) -> str:
        return self.data.get("date", "")

    def spot_token(self, index: str) -> str:
        """Fixed spot token from config (AGENTS.md §14)."""
        return str(self.cfg["indices"][index]["spot_token"])

    def expiries(self, index: str, on: date | None = None) -> list[date]:
        """Sorted option expiries >= on (default: all in the file)."""
        exps = [date.fromisoformat(e) for e in expiries(self.data["options"].get(index, []))]
        return [e for e in exps if on is None or e >= on]

    def option_tokens(self, index: str, expiry: date, atm: float, n: int) -> list[dict[str, Any]]:
        """CE/PE rows {token, strike, type} of one expiry: listed strike nearest atm, +/- n."""
        iso = expiry.isoformat()
        rows = [o for o in self.data["options"].get(index, []) if o["expiry"] == iso]
        strikes = sorted({o["strike"] for o in rows})
        if not strikes:
            return []
        i = strikes.index(min(strikes, key=lambda s: abs(s - atm)))
        keep = set(strikes[max(0, i - n): i + n + 1])
        return [{"token": o["token"], "strike": o["strike"], "type": o["type"]}
                for o in rows if o["strike"] in keep]

    def is_expiry_day(self, index: str, d: date) -> bool:
        """True if d is an option expiry of the index (from the master, never from weekdays)."""
        return d in set(self.expiries(index))

    def in_rollover(self, index: str, d: date, sessions: int) -> bool:
        """True if d is one of the last `sessions` trading sessions up to and including the
        index's nearest expiry (weekdays + holidays.yaml of the spot exchange)."""
        exps = self.expiries(index, on=d)
        hol = self.holidays.get(self.cfg["indices"][index]["spot_exchange"], set())
        if not exps or not is_trading_day(d, hol):
            return False
        count, day = 0, d
        while day <= exps[0]:
            count += is_trading_day(day, hol)
            day = date.fromordinal(day.toordinal() + 1)
        return count <= sessions

    def in_bnf_rollover(self, d: date) -> bool:
        """BANKNIFTY: last N sessions up to and including the monthly expiry."""
        return self.in_rollover("BANKNIFTY", d, self.cfg["oi"]["banknifty_monthly_sum_sessions"])

    def oi_expiries(self, index: str, d: date) -> list[date]:
        """Expiries whose OI is summed per strike on day d (current, plus next when required)."""
        exps = self.expiries(index, on=d)
        need_next = bool(exps) and self.cfg["oi"]["expiry_day_sum_next"] and exps[0] == d
        if index == "BANKNIFTY" and self.in_bnf_rollover(d):
            need_next = True
        return exps[:2] if need_next else exps[:1]


def ensure_instruments(cfg: dict[str, Any], root: Path, today: date,
                       runner: Any = None, log: Any = None) -> Instruments | None:
    """Return today's instruments, refreshing via a subprocess if the file is stale.

    On refresh failure, fall back to the old file if every index's nearest expiry is still
    >= today; otherwise return None (caller retries every refresh_retry_minutes).
    """
    import logging
    import subprocess
    import sys

    log = log or logging.getLogger("marketalert")
    icfg = cfg["instruments"]
    path = root / icfg["filtered_file"]
    holidays = load_holidays(root / cfg["schedule"]["holidays_file"])
    data = load_filtered(path, today)
    if data:
        return Instruments(data, cfg, holidays)
    cmd = [sys.executable, str(root / icfg["refresh_script"])]
    log.info("instruments stale; running %s", icfg["refresh_script"])
    try:
        res = (runner or subprocess.run)(cmd, cwd=str(root), timeout=icfg["refresh_timeout_seconds"],
                                         capture_output=True, text=True)
        ok = res.returncode == 0
        if not ok:
            log.error("instrument refresh failed (rc=%s): %s", res.returncode,
                      (res.stderr or res.stdout or "")[-300:])
    except Exception as e:  # noqa: BLE001 - includes subprocess.TimeoutExpired
        ok = False
        log.error("instrument refresh error: %s", e)
    data = load_filtered(path, today) if ok else None
    if data:
        return Instruments(data, cfg, holidays)
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = None
    if old:
        inst = Instruments(old, cfg, holidays)
        if all(inst.expiries(k, on=today) for k in cfg["indices"]):
            log.warning("using stale instruments from %s (nearest expiries still >= today)",
                        old.get("date"))
            return inst
    log.error("ALERT: no usable instruments file; retrying in %s min", icfg["refresh_retry_minutes"])
    return None


def wait_for_instruments(cfg: dict[str, Any], root: Path, today_fn: Any, sleep: Any = time.sleep,
                         stop: Any = None, runner: Any = None) -> Instruments | None:
    """Retry ensure_instruments every refresh_retry_minutes until success or stop() is true."""
    while True:
        inst = ensure_instruments(cfg, root, today_fn(), runner=runner)
        if inst or (stop and stop()):
            return inst
        sleep(cfg["instruments"]["refresh_retry_minutes"] * 60)
