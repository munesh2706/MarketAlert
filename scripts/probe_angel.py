"""M0 Angel SmartAPI capability probe.

Runs checks P1..P6 (each independent, try/except + timeout), never prints secrets,
writes reports/M0_probe.json. Total runtime is capped by probe.max_total_seconds.

Usage: python scripts/probe_angel.py
"""
from __future__ import annotations

import json
import statistics
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from marketalert.config import (  # noqa: E402
    ROOT, is_market_open, load_config, load_env, missing_env, now_ist, today_ist,
)

ANGEL_KEYS = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_MPIN", "ANGEL_TOTP_SECRET")
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
WS_EXCHANGE_TYPE = {"NSE": 1, "NFO": 2, "BSE": 3, "BFO": 4}

CFG = load_config()
ENV: dict[str, str] = {}
STATE: dict[str, Any] = {}      # shared results between checks (session, tokens, ...)
RESULTS: dict[str, dict[str, Any]] = {}
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


def parse_expiry(s: str) -> date | None:
    """Parse master expiry like '07OCT2026' into a date (locale independent)."""
    try:
        return date(int(s[5:9]), MONTHS[s[2:5].upper()], int(s[0:2]))
    except (KeyError, ValueError, IndexError):
        return None


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
    print(f"{name}: {res['status']} ({secs}s) {res.get('reason', '')}")


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
def load_master() -> list[dict[str, Any]]:
    import requests

    data_dir = ROOT / CFG["paths"]["data_dir"]
    data_dir.mkdir(exist_ok=True)
    cache = data_dir / f"scrip_master_{today_ist().isoformat()}.json"
    if not cache.exists():
        r = requests.get(CFG["instruments"]["master_url"],
                         timeout=CFG["instruments"]["download_timeout_seconds"])
        r.raise_for_status()
        tmp = cache.with_suffix(".tmp")
        tmp.write_bytes(r.content)
        tmp.replace(cache)
        for old in data_dir.glob("scrip_master_*.json"):
            if old != cache:
                old.unlink()
    with cache.open(encoding="utf-8") as f:
        return json.load(f)


def p2_instruments() -> dict[str, Any]:
    t = time.monotonic()
    master = load_master()
    load_s = round(time.monotonic() - t, 1)
    indices = CFG["indices"]
    div = CFG["instruments"]["strike_divisor"]
    today = today_ist()

    spot: dict[str, dict[str, str]] = {}
    opts: dict[str, list[dict[str, Any]]] = {k: [] for k in indices}
    name_to_key = {(v["option_exchange"], v["option_name"]): k for k, v in indices.items()}
    for row in master:
        seg, itype = row.get("exch_seg"), row.get("instrumenttype")
        if itype == "AMXIDX":
            for k, v in indices.items():
                if seg == v["spot_exchange"] and row.get("symbol", "").lower() == v["spot_symbol"].lower():
                    spot[k] = {"token": row["token"], "symbol": row["symbol"], "exchange": seg}
        elif itype == "OPTIDX":
            k = name_to_key.get((seg, row.get("name")))
            if k:
                exp = parse_expiry(row.get("expiry", ""))
                if exp and exp >= today:
                    opts[k].append({"token": row["token"], "symbol": row["symbol"], "expiry": exp,
                                    "strike": float(row["strike"]) / div,
                                    "type": row["symbol"][-2:], "lotsize": row.get("lotsize")})
    master_rows = len(master)
    del master

    # Filtered cache (small) for later phases / low-memory devices.
    small = {"spot": spot, "options": {k: [dict(o, expiry=o["expiry"].isoformat()) for o in v]
                                       for k, v in opts.items()}}
    (ROOT / CFG["paths"]["data_dir"] / "instruments_filtered.json").write_text(json.dumps(small))

    # ATM from current spot via REST LTP.
    ltp: dict[str, float] = {}
    ltp_err = None
    if STATE.get("api") and spot:
        try:
            req: dict[str, list[str]] = {}
            for s in spot.values():
                req.setdefault(s["exchange"], []).append(s["token"])
            data = market_data("LTP", req)
            by_token = {f["symbolToken"]: float(f["ltp"]) for f in data.get("fetched", [])}
            ltp = {k: by_token[s["token"]] for k, s in spot.items() if s["token"] in by_token}
        except Exception as e:  # noqa: BLE001
            ltp_err = scrub(e)
    else:
        ltp_err = "no login session (P1 failed)"

    out: dict[str, Any] = {"master_rows": master_rows, "master_load_seconds": load_s,
                           "spot_tokens": spot, "indices": {}}
    if ltp_err:
        out["ltp_error"] = ltp_err
    n_side = CFG["oi"]["strikes_each_side"]
    tokens: dict[str, dict[str, Any]] = {}
    ok = len(spot) == len(indices)
    for k, v in indices.items():
        expiries = sorted({o["expiry"] for o in opts[k]})
        info: dict[str, Any] = {"option_rows_future": len(opts[k]),
                                "nearest_expiry": expiries[0].isoformat() if expiries else None,
                                "next_expiry": expiries[1].isoformat() if len(expiries) > 1 else None,
                                "expiries_listed": len(expiries)}
        if not expiries:
            ok = False
        elif k in ltp:
            near = [o for o in opts[k] if o["expiry"] == expiries[0]]
            strikes = sorted({o["strike"] for o in near})
            step = v["strike_step"]
            atm = round(ltp[k] / step) * step
            if atm not in strikes:  # snap to nearest listed strike
                atm = min(strikes, key=lambda s: abs(s - ltp[k]))
            i = strikes.index(atm)
            sel = strikes[max(0, i - n_side): i + n_side + 1]
            chosen = [o for o in near if o["strike"] in sel]
            gaps = sorted({round(b - a, 2) for a, b in zip(sel, sel[1:])})
            info.update(spot_ltp=ltp[k], atm=atm, strikes_selected=len(sel),
                        strike_range=[sel[0], sel[-1]], strike_gaps=gaps,
                        ce_tokens=sum(o["type"] == "CE" for o in chosen),
                        pe_tokens=sum(o["type"] == "PE" for o in chosen),
                        sample_symbol=chosen[0]["symbol"] if chosen else None)
            tokens[k] = {"exchange": v["option_exchange"],
                         "tokens": [o["token"] for o in chosen],
                         "meta": {o["token"]: (o["strike"], o["type"]) for o in chosen}}
            if len(sel) < 2 * n_side + 1:
                ok = False
        else:
            ok = False
        out["indices"][k] = info
    STATE["spot"] = spot
    STATE["opt_tokens"] = tokens
    if not ok:
        out["_status"] = "PARTIAL"
    return out


# ---------------------------------------------------------------- P3 / P6
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
        oi_vals = [f.get("opnInterest") for f in fetched]
        nonnull = [x for x in oi_vals if x is not None]
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
    need("api", "opt_tokens")
    stats = poll_oi(list(STATE["opt_tokens"]))
    bad = [k for k, s in stats.items() if s["oi_nonnull"] == 0 or s["errors"]]
    missing = [k for k in CFG["indices"] if k not in stats]
    out: dict[str, Any] = {"per_index": stats}
    if missing:
        out["indices_without_tokens"] = missing
    if bad or missing:
        out["_status"] = "PARTIAL"
    return out


def p6_full_poll() -> dict[str, Any]:
    need("api", "opt_tokens")
    t = time.monotonic()
    stats = poll_oi(list(STATE["opt_tokens"]))
    total = round(time.monotonic() - t, 2)
    n = sum(s["tokens"] for s in stats.values())
    errs = sum(len(s["errors"]) for s in stats.values())
    return {"total_seconds": total, "tokens": n, "requests": sum(s["batches"] for s in stats.values()),
            "errors": errs, "oi_poll_seconds": CFG["oi"]["oi_poll_seconds"],
            "expiry_day_estimate_seconds": round(total * 2, 2),
            **({"_status": "PARTIAL"} if errs else {})}


# ---------------------------------------------------------------- P4
def p4_history() -> dict[str, Any]:
    need("api", "spot")
    days = CFG["ema"]["seed_trading_days"]
    end = now_ist()
    start = end - timedelta(days=CFG["probe"]["history_calendar_days"])
    out: dict[str, Any] = {}
    ok = True
    for k, s in STATE["spot"].items():
        try:
            resp = STATE["api"].getCandleData({
                "exchange": s["exchange"], "symboltoken": s["token"], "interval": "FIFTEEN_MINUTE",
                "fromdate": start.strftime("%Y-%m-%d %H:%M"), "todate": end.strftime("%Y-%m-%d %H:%M")})
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
        time.sleep(0.4)  # historical API rate limit ~3 req/s
    if not ok:
        out["_status"] = "PARTIAL"
    return out


# ---------------------------------------------------------------- P5
def p5_websocket() -> dict[str, Any]:
    if not is_market_open(CFG):
        raise SkipCheck("market closed")
    need("api", "spot", "feed")
    from SmartApi.smartWebSocketV2 import SmartWebSocketV2

    secs = CFG["probe"]["ws_seconds"]
    tok_to_key = {s["token"]: k for k, s in STATE["spot"].items()}
    ticks: dict[str, list[float]] = {k: [] for k in tok_to_key.values()}
    errors: list[str] = []
    ws = SmartWebSocketV2(STATE["jwt"], ENV["ANGEL_API_KEY"], ENV["ANGEL_CLIENT_CODE"], STATE["feed"])
    token_list: dict[int, list[str]] = {}
    for s in STATE["spot"].values():
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
                           "last_ltp": STATE.get("last_ws_ltp", {})}
    for k, ts in ticks.items():
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        out[k] = {"ticks": len(ts), "max_gap_seconds": round(max(gaps), 2) if gaps else None}
    if any(not ts for ts in ticks.values()):
        out["_status"] = "PARTIAL"
    return out


# ---------------------------------------------------------------- main
def main() -> int:
    global ENV
    ENV = load_env()
    miss = missing_env(ENV, ANGEL_KEYS)
    if miss:
        print("Missing in .env: " + ", ".join(miss))
        print("Create .env from .env.example, then run: python scripts/probe_angel.py")
        return 2

    started = now_ist()
    print(f"MarketAlert probe {started:%Y-%m-%d %H:%M:%S} IST (market open: {is_market_open(CFG)})")
    run_check("P1_login", p1_login, 30)
    run_check("P2_instruments", p2_instruments, 90)
    run_check("P3_oi_quotes", p3_oi, 45)
    run_check("P4_history_15m", p4_history, 30)
    run_check("P5_websocket", p5_websocket, CFG["probe"]["ws_seconds"] + 15)
    run_check("P6_full_oi_poll", p6_full_poll, 45)

    report = {"generated_ist": started.isoformat(), "total_seconds": round(time.monotonic() - T0, 1),
              "market_open": is_market_open(CFG, started), "checks": RESULTS}
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
