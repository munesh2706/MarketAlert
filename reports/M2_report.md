# M2 Report — alerts, Telegram, scheduler
Status: DONE
## Done
- alerts.py: ZONE, EMA (side + ⚡ confluence), BREAKOUT, BREAKOUT_FAILED, WALL_SHIFT, OI_CHANGE;
  per-(index, type, level) arm/disarm and re-arm; 09:45–14:45 window; daily cap; mutes; state in
  data/state.json (atomic, same-day reload, day reset, Telegram offset kept); map + summary formats.
- telegram.py: send with retry/backoff (429 retry_after), 1 msg/s send queue, getUpdates long-poll
  thread, persisted offset, TG_CHAT_ID only; /map /mute /unmute /status /help.
- scheduler.py: Schedule (weekends, holidays.yaml, next start) + DayRunner (08:50 refresh → 09:00
  start + "✅ MarketAlert started" → 09:20 holiday check → 09:45 map → 15:45 summary → sleep).
- main.py: Bot (forever loop, error → log + one "⚠️ MarketAlert error" + restart after 60 s,
  Ctrl+C), LiveSession (OI worker thread + queue, alerts on ticks/candles/OI), /status.
- replay: alert rules on recorded days with timestamps; `--telegram` sends "[REPLAY]" messages.
- scripts/run.py: full bot by default; `--smoke`, `--minutes N`, `--tg-test`, `--no-record`.
## Tests / checks
- `pytest tests -q` → 62 passed (no network): each alert type once + re-arm after the reset gap,
  level-change re-arm, window edges (09:44:59 / 09:45 / 14:44:59 / 14:45), cap, mute/unmute, state
  reload/reset, BREAKOUT_FAILED within 3 candles vs expired, confluence, WALL_SHIFT, OI_CHANGE
  30-min re-arm, map/summary text, OI units, scheduler (full day, weekend + holiday skip, no-ticks
  holiday, late start), command parsing + chat restriction + offset, send retry/429/400/never
  raises (token scrubbed), send-queue rate limit, OI worker survives errors.
- Bugs found by tests and fixed: re-arm ran after the rules (a due OI_CHANGE re-arm missed its
  snapshot); the summary listed unchanged strikes.
## Key results
- Telegram test "🧪 MarketAlert M2 test": sent OK.
- Live 2-min session (alerts to log only): OI poll 7.0 s in the worker thread; history seeding hit
  the rate limit repeatedly (many calls today) but recovered via 5/10/20 s backoff.
- Replay of that recording (2026-10-06, ~1.5 min): 1370 ticks, 3 OI snapshots, 0 alerts (correct:
  spot was not within any zone; nearest was NIFTY wall 22,700 at 13 pts vs zone 10).
## Assumptions
- BREAKOUT re-arm = price back ≥ reset gap on the other side of the level (not further out).
- Muted or capped alerts do not disarm (they can fire after an unmute while still valid).
- The multi-index map and summary start with a header line; every per-index alert, and /map <index>,
  starts with the index name.
- "From above/below" = spot ≥ EMA / spot < EMA when entering the zone.
- Holiday = listed for every tracked exchange (NSE and BSE). The no-data check ignores stale REST prices.
- Map and holiday check wait 120 s after the feed starts (late start gets ticks and an OI poll first).
## Problems
- Seeding many times a day trips Angel's history rate limit; normal use seeds once at 09:00 (then
  cache), so this is only a dev-time issue.
- Full Bot loop (scheduler + real Telegram) not run live here, to avoid sending more than the one
  authorised test message. Its parts are tested individually.
## Next — user action needed
1. PC: `git push`.
2. Phone (Termux):
       cd ~/MarketAlert && git pull
       python scripts/run.py --tg-test
       termux-wake-lock
       nohup python scripts/run.py > logs/bot.out 2>&1 &
   Check: `tail -f logs/marketalert.log`; in Telegram send /status. Stop: `pkill -f scripts/run.py`.
3. After a recorded day: `python scripts/replay.py <YYYY-MM-DD>` (add --telegram to resend).
## Questions
- None.
