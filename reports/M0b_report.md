# M0b Report — phone-friendly probe
Status: DONE (phone run pending)
## Done
- config: fixed `spot_token` per index (NIFTY 99926000, BANKNIFTY 99926009, SENSEX 99919000);
  instrument stream/parse settings; probe cap 420 s, P2 timeout 300 s; P4 rate-limit gap/retry.
- instruments.py: streamed download (256 KB chunks to `.part`, then rename), streaming JSON parser
  (object by object, no full load), atomic `instruments_filtered.json` with a `date` field,
  reuse-if-today, ATM ± N selection.
- probe: P4/P5 use config spot tokens only (run before P2); P2 reports download s, file MB,
  parse s, tracemalloc peaks, resource maxrss; reuses today's filtered file unless
  `--force-download`; P3/P6 read tokens from the filtered file (fresh or cached).
- P4: 1.0 s gap between history calls + one retry after a rate-limit error.
## Files
config.yaml, marketalert/instruments.py, scripts/probe_angel.py, reports/M0b_report.md
## Tests / checks
- py_compile OK; `pytest tests/test_config.py -q` → 7 passed.
- Streaming parser vs `json.load` on a real master: identical row and option counts for all 3 indices.
  Peak traced memory **8 MB streaming vs 170 MB json.load** (Windows: 4.1 s vs 2.2 s).
- Accidentally ran the full live probe once on Windows (74 s): a `.env` now exists here, and the
  run was meant to test the missing-.env path. No secrets in the output (checked).
## Key results (Windows live run, 2026-10-06 11:27 IST, market open)
| Check | Result |
|---|---|
| P1 login | PASS (bse_fo, nse_fo enabled) |
| P2 instruments | PASS: download 26.8 s, 34.1 MB, parse 4.0 s, peak 8 MB, filtered 0.66 MB |
| P3 OI | PASS: 62/62 OI non-null for each index, **SENSEX BFO OI available**; 0.5–0.8 s per batch |
| P4 history 15m | PARTIAL: NIFTY/SENSEX 16 days, 25 rows/day; BANKNIFTY rate-limited (fixed, not re-run) |
| P5 websocket | PASS: 20 s, ticks NIFTY 190 / BANKNIFTY 73 / SENSEX 18; max gap 1.2 / 1.2 / 3.0 s |
| P6 full OI poll | PASS: 186 tokens, 6 requests, 3.45 s (expiry day ≈ 7 s) vs 180 s interval |
- Nothing blocks the design. SENSEX ticks are sparser (~1 per 1–3 s), which is fine for 5/15-min candles.
## Assumptions
- P4/P5 moved ahead of P2 so a slow P2 cannot eat their time budget.
- The master is kept as a single overwritten `data/scrip_master.json`.
- `reports/M0_probe.json` is not committed (each device writes its own; avoids pull conflicts).
## Problems
- maxrss is unavailable on Windows (null); it will be reported on Termux.
## Next — user action needed
On the phone (Termux):
    cd ~/MarketAlert
    git pull
    python scripts/probe_angel.py
    cat reports/M0_probe.json
(Add `--force-download` to re-fetch the master if today's filtered file already exists.)
Share the JSON, especially P2 `parse_seconds`, `parse_peak_tracemalloc_mb` and `rss_peak_mb`.
## Questions
- None.
