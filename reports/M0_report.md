# M0 Report — scaffold + Angel capability probe
Status: PARTIAL (scaffold done; live probe blocked: no .env)
## Done
- Section 4 structure scaffolded: stub modules (docstrings only), .gitignore, requirements.txt,
  .env.example, holidays.yaml, README.md, git repo (branch `main`).
- config.yaml with all Section 5–10 values, grouped and commented (times quoted "HH:MM").
- marketalert/config.py: load_config, load_env, missing_env, now_ist/today_ist (fixed UTC+05:30
  from UTC, no tzdata/OS timezone), parse_hhmm, is_market_open.
- scripts/probe_angel.py: P1–P6, each in its own thread with try/except + timeout, 170 s total
  budget, secrets scrubbed from all output, writes reports/M0_probe.json.
## Files
config.yaml, holidays.yaml, requirements.txt, .env.example, .gitignore, README.md,
marketalert/*.py, scripts/{probe_angel,run,replay}.py, tests/test_config.py, reports/M0_report.md
## Tests / checks
- `pytest tests/test_config.py -q` → 7 passed.
- Probe without .env → exits 2 with "Missing in .env: ..." (names only).
- P2 dry run without login (public scrip master): parsing verified.
| Check | Result |
|---|---|
| P1 login TOTP | NOT RUN (no .env) |
| P2 instruments | PARTIAL — tokens/expiries OK; ATM needs login LTP |
| P3 OI quotes (SENSEX BFO OI) | NOT RUN |
| P4 15-min history | NOT RUN |
| P5 websocket | would SKIP today (Sunday, market closed) |
| P6 full OI poll timing | NOT RUN |
## Key results
- Spot tokens: Nifty 50 NSE 99926000, Nifty Bank NSE 99926009, SENSEX BSE 99919000.
- Expiries (from master, as of 2026-10-04): NIFTY 2026-10-06 / 10-13; BANKNIFTY 2026-10-27 /
  11-23 (Monday — holiday-shifted, confirms "never compute from weekdays"); SENSEX 2026-10-08 / 10-15.
- Strike field = strike × 100 for both NFO and BFO (divisor 100 correct). Listed strike steps
  NIFTY 50, BANKNIFTY/SENSEX 100 near ATM.
- SENSEX BFO OI availability and poll timing: unknown until probe runs (design-critical).
## Assumptions
- IST via fixed offset (no DST in India) instead of zoneinfo (tzdata missing on Windows/Termux).
- Index spot rows matched by instrumenttype AMXIDX + exchange + symbol (case-insensitive).
- OI via getMarketData mode FULL, field `opnInterest`; P3/P6 poll nearest expiry only;
  expiry-day load estimated as 2× P6.
- Master cached daily as data/scrip_master_YYYY-MM-DD.json plus a small instruments_filtered.json.
- Added a `.venv` for local dev; `.pytest_cache/` also gitignored.
## Problems
- **Memory risk on Termux**: scrip master is 33.8 MB / 144k rows; `json.load` of it likely
  exceeds the 150 MB budget on the phone. M1 should parse once per day in a short-lived step
  (or stream) and keep only instruments_filtered.json (0.8 MB) in the long-running process.
- SmartAPI SDK writes `logs/YYYY-MM-DD/app.log` relative to CWD and, on request exceptions,
  logs request headers (incl. bearer token). logs/ is gitignored; keep it private.
## Next — user action needed
1. Create `.env` from `.env.example` and fill all six keys, then run (ideally on a weekday
   09:15–15:30 IST so P5 is exercised):
   `.venv\Scripts\python scripts\probe_angel.py`  (Termux/Linux: `python scripts/probe_angel.py`)
   and share reports/M0_probe.json.
2. Create a private GitHub repo and push (needs GitHub CLI `gh` logged in):
   `gh repo create MarketAlert --private --source . --remote origin --push`
   or without gh: create private repo "MarketAlert" on github.com (no README), then
   `git remote add origin https://github.com/<your-username>/MarketAlert.git`
   `git push -u origin main`
## Questions
- OK to stream/parse the scrip master in a separate daily step to stay under 150 MB on Termux?
