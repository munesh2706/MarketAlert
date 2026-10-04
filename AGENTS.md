# AGENTS.md — MarketAlert

Read this file fully at the start of every session. It is the single source of truth.
If a phase prompt conflicts with this file, the phase prompt wins for that phase only.

## 1. Goal

Information-only Telegram alert bot for NIFTY, BANKNIFTY and SENSEX. It tracks the strongest
option-OI walls/floors, 15-minute EMA50/EMA200, and sends alerts so the user does not have to
watch charts. It never places orders and never claims to predict direction.

Runs unattended on an Android phone (Termux), and must also run unchanged on Windows and Linux.

## 2. Working rules (token economy — mandatory)

1. Do only what the current phase prompt asks. No extras or refactors outside scope.
2. Never print secrets. Credentials come from `.env` (python-dotenv). Never commit `.env`.
3. Do not run anything longer than 3 minutes. Write the script, test on a tiny slice, give the user the command.
4. Run only targeted tests (`pytest tests/test_x.py -q`). Tiny fixtures.
5. If the spec is ambiguous, choose the simplest sensible option and list it under "Assumptions".
6. All tunable values live in `config.yaml`. No magic numbers in code.
7. Python 3.11+, `pathlib`, type hints on public functions, short docstrings.
8. End of every phase: `git add -A && git commit -m "MXX: <summary>"`, write the report (Section 12)
   to `reports/MXX_report.md`, print it as the final output.

## 3. Platform constraints (Android/Termux compatible)

- Pure Python plus light libraries only: smartapi-python, pyotp, python-dotenv, requests,
  websocket-client, logzero, pyyaml, pytest (dev). **No pandas, numpy, pyarrow, or compiled extensions.**
- Low memory (< 150 MB), low CPU. No GUI. Local time is IST (Asia/Kolkata); never assume the
  OS timezone — compute IST explicitly.
- Network failures are normal: every external call has a timeout, retries with backoff, and the
  bot keeps running. WebSocket auto-reconnects.
- State that must survive a restart (alert arm/disarm state, mutes, last walls) is saved to
  `data/state.json` atomically.

## 4. Folder structure

```
AGENTS.md  README.md  requirements.txt  config.yaml  .env.example  .gitignore
marketalert/
  __init__.py
  main.py          # entry point: scheduler loop
  config.py        # load config.yaml + .env, IST helpers
  angel.py         # login (TOTP), quotes/OI, historical candles, websocket, re-login
  instruments.py   # scrip master download/cache, index tokens, option tokens, expiry selection
  candles.py       # build 1/5/15-min candles from ticks
  indicators.py    # EMA (pure python)
  walls.py         # OI snapshot, walls/floors, shift confirmation, OI change
  alerts.py        # alert rules, zones, arm/re-arm state, cooldowns, caps
  telegram.py      # send messages, command polling (/map /mute /status /help)
  scheduler.py     # daily timeline, holidays
  recorder.py      # save live ticks + OI snapshots to data/recordings/
  replay.py        # run the engine on recorded days, print alerts
scripts/           # probe_angel.py, run.py, replay.py
tests/
data/              # gitignored: instrument cache, recordings, state.json
logs/              # gitignored
reports/
```

## 5. Indices (config `indices:`)

| key | spot | exchange (options) | option expiry | strike step | zone | reset gap | breakout buffer |
|---|---|---|---|---|---|---|---|
| NIFTY | Nifty 50 (NSE) | NFO | weekly, Tuesday | 50 | ±10 | 30 | 5 |
| BANKNIFTY | Nifty Bank (NSE) | NFO | monthly, last Tuesday | 100 | ±25 | 75 | 15 |
| SENSEX | Sensex (BSE) | BFO | weekly, Thursday | 100 | ±25 | 75 | 15 |

- All prices are the **spot index**. Walls are spot strikes; compare directly with spot.
- Expiry is taken from the instrument master (nearest expiry ≥ today), never computed from weekdays.
- Expiry-day OI = current expiry + next expiry summed per strike. BANKNIFTY: last 3 sessions
  before monthly expiry also sum current + next month.
- Strikes fetched: ATM ± `strikes_each_side` (15). The outermost fetched strike can never be a wall.

## 6. Data

- Login: SmartAPI with TOTP (pyotp). Re-login automatically on session expiry.
- Live price: WebSocket LTP for the 3 spot index tokens. Fallback: REST LTP poll every 5 s if the
  WebSocket is down > 30 s.
- OI: market-data API (OI field) for all option tokens, batches ≤ 50, every `oi_poll_seconds` (180).
- 15-min EMA50/EMA200 on spot: seed from historical 15-min candles (≥ 12 trading days) at startup;
  update on each completed 15-min candle built from ticks. Live EMA value for proximity checks =
  EMA of completed candles (do not include the forming candle).
- 5-min candles from ticks for breakout checks.

## 7. Walls

- Main wall = CE strike with highest OI at/above spot; 2nd wall = next highest.
- Main floor = PE strike with highest OI at/below spot; 2nd floor = next highest.
- WALL_SHIFT confirmed only when the new main strike persists for 2 consecutive polls.
- OI change % at a strike over 30 min uses snapshots ≥ 30 min apart (or earliest of the day after
  09:30; never a baseline before 09:30).

## 8. Alerts (window 09:45–14:45 IST; config `alerts:`)

| Type | Trigger | Re-arm |
|---|---|---|
| ZONE | spot within zone of main wall or main floor | price moves ≥ reset gap away from that level, or level strike changes |
| EMA | spot within zone of 15-min EMA50 or EMA200; message states side (from above / below); flag ⚡ confluence if a main/2nd wall or floor lies within zone of the EMA | price moves ≥ reset gap away from the EMA value |
| BREAKOUT | completed 5-min candle closes beyond main wall (up) or floor (down) by breakout buffer | per level and direction, after reset gap away |
| BREAKOUT_FAILED | after BREAKOUT, a 5-min close back inside within 3 five-min candles | once per breakout |
| WALL_SHIFT | main wall or floor strike changes (confirmed) | each change |
| OI_CHANGE | main wall/floor OI changes ≥ `oi_change_pct` (10) over 30 min | once per level per 30 min |

- **One alert per instance**: while disarmed, the same (index, type, level) never alerts again.
- Daily cap per index (`max_alerts_per_index`: 40). Mutes via Telegram.
- Every message starts with the index name and includes spot price and distance to the level.

## 9. Schedule (config `schedule:`)

- 09:00 start: login, load instruments, seed EMAs, start WebSocket, Telegram health ping.
- 09:00–09:45: track silently (walls, candles, OI history).
- 09:45: market map per index (spot, main/2nd walls and floors with OI, PCR, EMA50/EMA200, expiry).
- 09:45–14:45: alerts.
- 15:45: market summary per index (open/high/low/close, how walls/floors moved, largest OI changes,
  PCR start vs end, alerts sent). Then sleep until next trading day 09:00.
- Holidays: `holidays.yaml` (user-maintained list per exchange). Also: if no ticks by 09:20, treat as
  holiday, send one message, sleep.
- Weekends skipped.

## 10. Telegram

- One chat (`TG_CHAT_ID`). Accept commands only from that chat.
- Commands: `/map <index|all>`, `/mute <index|all> <minutes>`, `/unmute <index|all>`, `/status`, `/help`.
- Sending failures are retried and never crash the bot.

## 11. Recording and replay

- Recorder saves each day's ticks (spot LTP) and OI snapshots to `data/recordings/YYYY-MM-DD/` as
  compact JSON lines.
- Replay runs the same engine on a recorded day with a simulated clock and prints the alerts that
  would have been sent (no Telegram unless `--telegram`).

## 12. Report template (≤ 60 lines; save to reports/MXX_report.md and print)

```
# MXX Report — <title>
Status: DONE | PARTIAL | BLOCKED
## Done
## Files
## Tests / checks
## Key results
## Assumptions
## Problems
## Next — user action needed
## Questions
```

## 13. Credentials (.env)

ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_MPIN, ANGEL_TOTP_SECRET, TG_BOT_TOKEN, TG_CHAT_ID
