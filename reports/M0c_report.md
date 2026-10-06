# M0c Report — apply probe findings
Status: DONE
## Done
- AGENTS.md: new Section 14 "Probe findings (M0, binding)" records the PC and phone measurements
  and the rules that follow from them (refresh in a separate process, no symbol parsing,
  history throttle and EMA fallback, websocket reconnect).
- config.yaml:
  - new `history:` section: 1.5 s gap, 3 retries, backoff 2/4/8 s, re-seed every 5 min
  - new `websocket:` section: SDK retries plus an outer reconnect backoff
  - `instruments.refresh_timeout_seconds: 300`
  - removed the probe-only history and P2 timeout keys
- instruments.py: `option_type()` is the only use of the trading symbol (validated `CE`/`PE`
  suffix); strike and expiry come from master fields only; bad rows are dropped and counted.
- probe: P4 uses `get_candles()` with backoff retries and the `history:` spacing, timeout raised
  to 75 s; P5 uses the configured SDK retries and reports `reconnect_attempts`; P2 timeout comes
  from `refresh_timeout_seconds`.
## Files
AGENTS.md, config.yaml, marketalert/instruments.py, scripts/probe_angel.py, reports/M0c_report.md
## Tests / checks
- py_compile OK; `pytest tests/test_config.py -q` → 7 passed.
- Offline: rebuilt the filtered file from the cached master with the CE/PE check → identical
  options, 0 rows skipped. `option_type` checked on NIFTY, SENSEX weekly and future symbols.
- Offline: `get_candles` with a fake API recovers after 2 rate-limit errors and gives up after 3 retries.
- No live run (not needed for these changes).
## Key results (from user's runs)
- SENSEX BFO OI available. Full OI poll: PC 2.8 s, phone 10.1 s (vs 180 s interval).
- Phone instrument refresh: 18.6 s with --force-download (limit 300 s).
- Phone P4 PARTIAL from the rate limit → spacing raised to 1.5 s with 3 retries.
## Assumptions
- The master has no CE/PE field (`instrumenttype` = `OPTIDX`), so a validated symbol suffix is
  the only possible source of option type. All other symbol parsing is banned.
- The bot-side behaviour (start without an index's EMA, re-seed every 5 min, outer websocket
  reconnect loop, separate refresh process) is specified in AGENTS.md §14 and config, and will be
  built in the phases that implement angel.py, indicators.py and main.py/scheduler.py.
## Problems
- SmartWebSocketV2 retries are bounded and its counter never resets, so the bot must own reconnects.
## Next — user action needed
- PC: `git push`. Optional phone re-check of P4: `git pull`, then `python scripts/probe_angel.py`.
## Questions
- None.
