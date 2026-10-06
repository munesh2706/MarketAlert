"""M0 Angel SmartAPI capability probe (phone friendly).

Runs checks P1..P6 (each independent, try/except + timeout), never prints secrets,
writes reports/M0_probe.json. Total runtime is capped by probe.max_total_seconds.
Spot tokens come from config (P4/P5 never depend on P2). P2 streams the scrip master
to disk and writes data/instruments_filtered.json; P3/P6 read option tokens from it.

Usage: python scripts/probe_angel.py [--force-download]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
import tracemalloc
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from marketalert.config import (  # noqa: E402
    ROOT, is_market_open, load_config, load_env, missing_env, now_ist, today_ist,
)
from marketalert import instruments as ins  # noqa: E402

ANGEL_KEYS = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_MPIN", "ANGEL_TOTP_SECRET")
WS_EXCHANGE_TYPE = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4}

CFG = load_config()
SPOT = {k: {"token": str(v["spot_token"]), "exchange": v["spot_exchange"]}
        for k, v in CFG["indices"].items()}
ENV: dict[str, str] = {}
STATE: dict[str, Any] = {}      # shared results between checks (session, tokens, ...)
RESULTS: dict[str, dict[str, Any]] = {}
ARGS = argparse.Namespace(force_download=False)
T0 = time.monotonic()


# ---------------------------------------------------------------- helpers
def scrub(text: Any) -> str:
    """Remove any credential / session token values from a string."""
    s = str(text)
    secrets = [v for v in ENV.values() if v and len(v) >= 4]
    secrets += [v for v in (STATE.get("jwt"), STATE.get("feed"), STATE.get("refresh")) if v]
    for v in secrets:
        s = s.replace(v, "***")
    return s[:300]


def rss_peak_mb() -> float | None:
    """Process peak RSS in MB via resource.getrusage (None where unavailable, e.g. Windows)."""
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1e6 if sys.platform == "darwin" else 1024), 1)


def run_check(name: str, fn: Callable[[], dict[str, Any]], timeout: float | None = None) -> None:
    """Run fn in a thread with a timeout; record PASS/FAIL/SKIP + details."""
    budget = CFG["probe"]["max_total_seconds"] - (time.monotonic() - T0)
    timeout = min(timeout or CFG["probe"]["check_timeout_seconds"], budget)
    if timeout <= 1:
        RESULTS[name] = {"status": "SKIP", "reason": "total time budget exhausted"}
        print(f"{name}: SKIP (time budget)")
        return
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["details"] = fn()
        except SkipCheck as e:
            box["skip"] = str(e)
        except Exception as e:  # noqa: BLE001 - probe must never crash
            box["error"] = f"{type(e).__name__}: {scrub(e)}"

    print(f"{name}: running (timeout {timeout:.0f}s)...", flush=True)
    t = time.monotonic()
    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(timeout)
    secs = round(time.monotonic() - t, 2)
    if th.is_alive():
        res = {"status": "FAIL", "reason": f"timeout after {timeout:.0f}s"}
    elif "skip" in box:
        res = {"status": "SKIP", "reason": box["skip"]}
    elif "error" in box:
        res = {"status": "FAIL", "reason": box["error"]}
    else:
        d = box.get("details", {})
        res = {"status": d.pop("_status", "PASS"), "details": d}
    res["seconds"] = secs
    RESULTS[name] = res
    print(f"{name}: {res['status']} ({secs}s) {res.get('reason', '')}", flush=True)


class SkipCheck(Exception):
    """Raised to mark a check as SKIP."""


def need(*keys: str) -> None:
    for k in keys:
        if not STATE.get(k):
            raise SkipCheck(f"needs {k} from an earlier check")


def chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def market_data(mode: str, exch_tokens: dict[str, list[str]]) -> dict[str, Any]:
    resp = STATE["api"].getMarketData(mode, exch_tokens)
    if not resp or not resp.get("status"):
        msg = (resp or {}).get("message", "no response")
        code = (resp or {}).get("errorcode", "")
        raise RuntimeError(f"getMarketData failed: {code} {scrub(msg)}")
    return resp.get("data") or {}


# ---------------------------------------------------------------- P1
def p1_login() -> dict[str, Any]:
    import pyotp
    from SmartApi import SmartConnect

    api = SmartConnect(api_key=ENV["ANGEL_API_KEY"], timeout=CFG["network"]["timeout_seconds"])
    totp = pyotp.TOTP(ENV["ANGEL_TOTP_SECRET"]).now()
    resp = api.generateSession(ENV["ANGEL_CLIENT_CODE"], ENV["ANGEL_MPIN"], totp)
    if not resp or not resp.get("status"):
        raise RuntimeError(f"login failed: {(resp or {}).get('errorcode', '')} "
                           f"{scrub((resp or {}).get('message', 'no response'))}")
    STATE.update(api=api, jwt=api.access_token, feed=api.feed_token, refresh=api.refresh_token)
    exchanges = (resp.get("data") or {}).get("exchanges", [])
    return {"logged_in": True, "feed_token_present": bool(api.feed_token), "exchanges": exchanges}


# ---------------------------------------------------------------- P2
def p2_instruments() -> dict[str, Any]:
    icfg = CFG["instruments"]
    master = ROOT / icfg["master_file"]
    filtered = ROOT / icfg["filtered_file"]
    master.parent.mkdir(parents=True, exist_ok=True)
    today = today_ist()
    out: dict[str, Any] = {}

    data = None if ARGS.force_download else ins.load_filtered(filtered, today)
    if data:
        out["cached"] = True
    else:
        out["cached"] = False
        tracemalloc.start()
        dl = ins.download_master(icfg["master_url"], master, icfg["download_timeout_seconds"],
                                 icfg["download_chunk_bytes"])
        out.update(download_seconds=dl["seconds"], master_file_mb=dl["mb"],
                   download_peak_tracemalloc_mb=round(tracemalloc.get_traced_memory()[1] / 1e6, 1))
        tracemalloc.reset_peak()
        t = time.monotonic()
        data = ins.build_filtered(master, CFG["indices"], icfg["strike_divisor"], today,
                                  icfg["parse_chunk_chars"])
        out["parse_seconds"] = round(time.monotonic() - t, 1)
        out["parse_peak_tracemalloc_mb"] = round(tracemalloc.get_traced_memory()[1] / 1e6, 1)
        tracemalloc.stop()
        ins.write_json_atomic(filtered, data)
        out["master_rows"] = data["master_rows"]
        out["skipped_option_rows"] = data["skipped_option_rows"]

    out["filtered_file_mb"] = round(filtered.stat().st_size / 1e6, 2)
    out["rss_peak_mb"] = rss_peak_mb()
    out["filtered_date"] = data["date"]
    out["master_spot_rows"] = data["spot"]   # cross-check of config spot tokens
    ok = True
    per: dict[str, Any] = {}
    for k in CFG["indices"]:
        opts = data["options"].get(k, [])
        exps = ins.expiries(opts)
        per[k] = {"option_rows_future": len(opts),
                  "nearest_expiry": exps[0] if exps else None,
                  "next_expiry": exps[1] if len(exps) > 1 else None,
                  "expiries_listed": len(exps),
                  "spot_token_in_master": k in data["spot"]}
        ok = ok and bool(exps)
    out["indices"] = per
    if not ok:
        out["_status"] = "PARTIAL"
    return out


# ---------------------------------------------------------------- P3 / P6
def ensure_option_tokens() -> dict[str, Any]:
    """Load today's filtered file, get spot LTP (config tokens), select ATM +/- N tokens."""
    if STATE.get("opt_tokens"):
        return STATE["opt_info"]
    need("api")
    data = ins.load_filtered(ROOT / CFG["instruments"]["filtered_file"], today_ist())
    if not data:
        raise SkipCheck("no instruments_filtered.json for today (run P2 / --force-download)")
    req: dict[str, list[str]] = {}
    for s in SPOT.values():
        req.setdefault(s["exchange"], []).append(s["token"])
    fetched = market_data("LTP", req).get("fetched", [])
    by_token = {f["symbolToken"]: float(f["ltp"]) for f in fetched}
    n_side = CFG["oi"]["strikes_each_side"]
    tokens: dict[str, dict[str, Any]] = {}
    info: dict[str, Any] = {}
    for k, v in CFG["indices"].items():
        ltp = by_token.get(SPOT[k]["token"])
        if ltp is None:
            info[k] = {"error": "no spot LTP"}
            continue
        sel = ins.select_atm_options(data["options"].get(k, []), ltp, v["strike_step"], n_side)
        st = sel["strikes"]
        info[k] = {"spot_ltp": ltp, "atm": sel["atm"], "expiry": sel["expiry"],
                   "strikes_selected": len(st), "strike_range": [st[0], st[-1]] if st else None,
                   "ce_tokens": sum(o["type"] == "CE" for o in sel["rows"]),
                   "pe_tokens": sum(o["type"] == "PE" for o in sel["rows"])}
        if sel["rows"]:
            tokens[k] = {"exchange": v["option_exchange"], "tokens": [o["token"] for o in sel["rows"]]}
    STATE["opt_tokens"], STATE["opt_info"] = tokens, info
    if not tokens:
        raise RuntimeError(f"no option tokens selected: {info}")
    return info


def poll_oi(index_keys: list[str]) -> dict[str, Any]:
    """Fetch FULL quotes for option tokens of the given indices in batches; return stats."""
    bs = CFG["oi"]["quote_batch_size"]
    stats: dict[str, Any] = {}
    for k in index_keys:
        entry = STATE["opt_tokens"][k]
        batch_secs, errors, fetched, unfetched = [], [], [], 0
        for batch in chunks(entry["tokens"], bs):
            t = time.monotonic()
            try:
                data = market_data("FULL", {entry["exchange"]: batch})
                fetched += data.get("fetched", [])
                unfetched += len(data.get("unfetched") or [])
            except Exception as e:  # noqa: BLE001
                errors.append(scrub(e))
            batch_secs.append(round(time.monotonic() - t, 2))
        nonnull = [x for x in (f.get("opnInterest") for f in fetched) if x is not None]
        stats[k] = {"tokens": len(entry["tokens"]), "fetched": len(fetched), "unfetched": unfetched,
                    "oi_nonnull": len(nonnull), "oi_positive": sum(1 for x in nonnull if x and x > 0),
                    "batches": len(batch_secs), "seconds_per_batch": batch_secs,
                    "errors": errors, "rate_limited": any("rate" in e.lower() or "access denied"
                                                          in e.lower() for e in errors)}
        if fetched:
            top = max(fetched, key=lambda f: f.get("opnInterest") or 0)
            stats[k]["max_oi_symbol"] = top.get("tradingSymbol")
            stats[k]["max_oi"] = top.get("opnInterest")
    return stats


def p3_oi() -> dict[str, Any]:
    info = ensure_option_tokens()
    stats = poll_oi(list(STATE["opt_tokens"]))
    bad = [k for k, s in stats.items() if s["oi_nonnull"] == 0 or s["errors"]]
    missing = [k for k in CFG["indices"] if k not in stats]
    out: dict[str, Any] = {"selection": info, "per_index": stats}
    if missing:
        out["indices_without_tokens"] = missing
    if bad or missing:
        out["_status"] = "PARTIAL"
    return out


def p6_full_poll() -> dict[str, Any]:
    ensure_option_tokens()
    t = time.monotonic()
    stats = poll_oi(list(STATE["opt_tokens"]))
    total = round(time.monotonic() - t, 2)
    errs = sum(len(s["errors"]) for s in stats.values())
    return {"total_seconds": total, "tokens": sum(s["tokens"] for s in stats.values()),
            "requests": sum(s["batches"] for s in stats.values()), "errors": errs,
            "oi_poll_seconds": CFG["oi"]["oi_poll_seconds"],
            "expiry_day_estimate_seconds": round(total * 2, 2),
            **({"_status": "PARTIAL"} if errs else {})}


# ---------------------------------------------------------------- P4
def get_candles(params: dict[str, str], key: str, out: dict[str, Any]) -> dict[str, Any]:
    """getCandleData with backoff retries on rate-limit errors (SDK raises on those replies)."""
    h = CFG["history"]
    delay = h["retry_backoff_seconds"]
    for attempt in range(h["retries"] + 1):
        try:
            return STATE["api"].getCandleData(params)
        except Exception as e:  # noqa: BLE001
            if "access rate" not in str(e).lower() or attempt == h["retries"]:
                raise
            out.setdefault("rate_limit_retries", []).append(key)
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def p4_history() -> dict[str, Any]:
    need("api")
    days = CFG["ema"]["seed_trading_days"]
    end = now_ist()
    start = end - timedelta(days=CFG["probe"]["history_calendar_days"])
    out: dict[str, Any] = {}
    ok = True
    for k, s in SPOT.items():
        params = {"exchange": s["exchange"], "symboltoken": s["token"], "interval": "FIFTEEN_MINUTE",
                  "fromdate": start.strftime("%Y-%m-%d %H:%M"), "todate": end.strftime("%Y-%m-%d %H:%M")}
        try:
            resp = get_candles(params, k, out)
            if not resp or not resp.get("status"):
                raise RuntimeError(f"{(resp or {}).get('errorcode', '')} "
                                   f"{scrub((resp or {}).get('message', 'no response'))}")
            rows = resp.get("data") or []
            dates = sorted({r[0][:10] for r in rows})
            keep = set(dates[-days:])
            kept = [r for r in rows if r[0][:10] in keep]
            per_day = [sum(1 for r in kept if r[0][:10] == d) for d in sorted(keep)]
            out[k] = {"rows_fetched": len(rows), "trading_days": len(dates),
                      "rows_last_n_days": len(kept),
                      "first": kept[0][0] if kept else None, "last": kept[-1][0] if kept else None,
                      "rows_per_day_median": statistics.median(per_day) if per_day else 0}
            if len(dates) < days:
                ok = False
        except Exception as e:  # noqa: BLE001
            out[k] = {"error": scrub(e)}
            ok = False
        time.sleep(CFG["history"]["request_gap_seconds"])
    if not ok:
        out["_status"] = "PARTIAL"
    return out


# ---------------------------------------------------------------- P5
def p5_websocket() -> dict[str, Any]:
    if not is_market_open(CFG):
        raise SkipCheck("market closed")
    need("api", "feed")
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2

    secs = CFG["probe"]["ws_seconds"]
    tok_to_key = {s["token"]: k for k, s in SPOT.items()}
    ticks: dict[str, list[float]] = {k: [] for k in SPOT}
    errors: list[str] = []
    wcfg = CFG["websocket"]
    ws = SmartWebSocketV2(STATE["jwt"], ENV["ANGEL_API_KEY"], ENV["ANGEL_CLIENT_CODE"], STATE["feed"],
                          max_retry_attempt=wcfg["sdk_max_retry_attempts"],
                          retry_delay=wcfg["sdk_retry_delay_seconds"])
    token_list: dict[int, list[str]] = {}
    for s in SPOT.values():
        token_list.setdefault(WS_EXCHANGE_TYPE[s["exchange"]], []).append(s["token"])

    def on_open(_wsapp):
        ws.subscribe("probe0001", 1,
                     [{"exchangeType": et, "tokens": toks} for et, toks in token_list.items()])

    def on_data(_wsapp, msg):
        k = tok_to_key.get(str(msg.get("token", "")).strip("\x00"))
        if k:
            ticks[k].append(time.monotonic())
            STATE.setdefault("last_ws_ltp", {})[k] = msg.get("last_traded_price", 0) / 100

    def on_error(*args):
        errors.append(scrub(args[-1] if args else "error"))

    ws.on_open, ws.on_data, ws.on_error = on_open, on_data, on_error
    th = threading.Thread(target=ws.connect, daemon=True)
    th.start()
    time.sleep(secs)
    try:
        ws.close_connection()
    except Exception:  # noqa: BLE001
        pass
    out: dict[str, Any] = {"listen_seconds": secs, "errors": errors[:5],
                           "reconnect_attempts": getattr(ws, "current_retry_attempt", None),
                           "last_ltp": STATE.get("last_ws_ltp", {})}
    for k, ts in ticks.items():
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        out[k] = {"ticks": len(ts), "max_gap_seconds": round(max(gaps), 2) if gaps else None}
    if any(not ts for ts in ticks.values()):
        out["_status"] = "PARTIAL"
    return out


# ---------------------------------------------------------------- main
def main() -> int:
    global ENV, ARGS
    ap = argparse.ArgumentParser(description="Angel SmartAPI capability probe")
    ap.add_argument("--force-download", action="store_true",
                    help="re-download the scrip master even if today's filtered file exists")
    ARGS = ap.parse_args()
    ENV = load_env()
    miss = missing_env(ENV, ANGEL_KEYS)
    if miss:
        print("Missing in .env: " + ", ".join(miss))
        print("Create .env from .env.example, then run: python scripts/probe_angel.py")
        return 2

    started = now_ist()
    print(f"MarketAlert probe {started:%Y-%m-%d %H:%M:%S} IST (market open: {is_market_open(CFG)}, "
          f"cap {CFG['probe']['max_total_seconds']}s)")
    run_check("P1_login", p1_login, 30)
    run_check("P4_history_15m", p4_history, 75)
    run_check("P5_websocket", p5_websocket, CFG["probe"]["ws_seconds"] + 15)
    run_check("P2_instruments", p2_instruments, CFG["instruments"]["refresh_timeout_seconds"])
    run_check("P3_oi_quotes", p3_oi, 45)
    run_check("P6_full_oi_poll", p6_full_poll, 45)

    report = {"generated_ist": started.isoformat(), "total_seconds": round(time.monotonic() - T0, 1),
              "market_open": is_market_open(CFG, started), "rss_peak_mb": rss_peak_mb(),
              "checks": RESULTS}
    out = ROOT / CFG["paths"]["reports_dir"] / "M0_probe.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(scrub_json(report), encoding="utf-8")
    print(f"Wrote {out.relative_to(ROOT)} in {report['total_seconds']}s")
    return 0


def scrub_json(obj: Any) -> str:
    text = json.dumps(obj, indent=2, default=str)
    for v in [*ENV.values(), STATE.get("jwt"), STATE.get("feed"), STATE.get("refresh")]:
        if v and len(v) >= 4:
            text = text.replace(v, "***")
    return text


if __name__ == "__main__":
    sys.exit(main())
