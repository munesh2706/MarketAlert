# M1 Report — core engine
Status: DONE
## Done
- config: history spacing 3 s, 4 retries, backoff 5/10/20/40 s, reseed 5 min, cache file;
  `angel:` (rate-limit text, session codes, retries, login retry); websocket loop settings; logging; replay.
- instruments.py: `Instruments` (spot_token, expiries, option_tokens, is_expiry_day,
  in_bnf_rollover, oi_expiries); `ensure_instruments` runs scripts/refresh_instruments.py as a
  subprocess if stale, falls back to the old file while its expiries are valid, else retries every 2 min.
- candles.py (1/5/15 min, 09:15-aligned, carried closes), indicators.py (EMA, SMA seed, None until
  seeded), walls.py (walls/floors, edge exclusion, PCR, summing, WallTracker, OiHistory).
- angel.py: AngelClient (TOTP login with network retries, re-login on session errors, rate-limit
  backoff on all REST calls, OI batches ≤ 50, 15-min history with same-day cache); LiveFeed (own
  reconnect loop, REST fallback, forced reconnect on stale socket); SDK logs to logs/ only, redacted.
- engine.py (new): IndexState/Engine shared by live and replay. recorder.py, replay.py,
  scripts/replay.py, scripts/run.py (`--smoke`, `--minutes N`), main.py LiveSession.
- AGENTS.md §4 (engine.py, refresh script) and §14 (history, rate limit, cache, refresh, websocket).
## Tests / checks
- `pytest tests -q` → 39 passed (no network): expiry/rollover incl. holiday-shifted expiry and a
  holiday inside the window, candles with gaps, EMA vs hand values, walls/edge/summing, shift
  confirmation, OI baseline, rate-limit backoff (quotes + history), re-login, secret scrubbing,
  history cache reuse + incremental fetch, websocket drop→reconnect→fallback→resume, stale-socket
  reconnect, login retry, log redaction, record→replay round trip.
## Key results (live, PC, 2026-10-06 ~12:00 IST, market open)
- Smoke 1: crashed on a login read timeout → fixed (login network retries + live login retry loop).
- Smoke 2: 3 EMAs seeded (286 candles / 12 days each), OI 62/62 per index, poll 8.05 s, total 25 s.
  NIFTY summed 10-06 + 10-13 (its expiry day).
- Smoke 3 (same day): history cache hit for all 3 indices, no history calls, total 14.6 s.
- 1-min live session (`--minutes 1 --no-record`): 853 websocket ticks, 0 reconnects, OI poll 10.7 s.
## Assumptions
- Added marketalert/engine.py (the folder structure had no home for the engine core).
- Rollover = the date is one of the last 3 trading sessions up to and including the nearest
  BANKNIFTY expiry; holidays come from the spot exchange's list in holidays.yaml.
- Candles: ticks outside 09:15–15:30 are ignored; the first real tick in a tick-less minute
  replaces the carried close; no overnight fill.
- Walls: both outermost fetched strikes are excluded; ties go to the strike nearer spot.
- OI-change baseline = the latest same-day snapshot that is ≥ 30 min old and at/after 09:30, else None.
- Recordings store the summed snapshot the engine used, plus the EMA seed (`{index}_seed.json`),
  so replay is self-contained. Replay prints a timeline line per completed 15-min candle and each shift.
## Problems
- The SmartAPI SDK writes request headers (API key, Bearer token) to logs/YYYY-MM-DD/app.log on errors.
  Now redacted by a logging filter. One pre-filter line from this session was redacted in place.
  Check older app.log files on the phone (from the probe runs) and delete them.
- An OI poll blocks the main loop about 10 s; ticks keep flowing (websocket thread), only the
  candle clock and fallback check pause. Fine for M1; revisit if needed.
## Next — user action needed
1. PC: `git push`.
2. Phone (Termux), during market hours:
       cd ~/MarketAlert && git pull
       termux-wake-lock
       python scripts/run.py --minutes 120
   (Ctrl+C stops cleanly.) Recordings go to data/recordings/<date>/. Replay later:
       python scripts/replay.py <YYYY-MM-DD>
3. Optional: `rm -r logs/2026-*` on the phone to remove older SDK logs that may hold credentials.
## Questions
- None.
