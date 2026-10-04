# MarketAlert

Information-only Telegram alert bot for NIFTY, BANKNIFTY and SENSEX. It tracks the strongest
option-OI walls/floors and 15-minute EMA50/EMA200 on the spot index and sends Telegram alerts
during market hours. It never places orders and never predicts direction. Runs on Android
(Termux), Windows and Linux with pure-Python dependencies. See `AGENTS.md` for the full spec.

## Setup

_TODO (later phase)._ For now:

    python -m venv .venv
    pip install -r requirements.txt
    cp .env.example .env      # fill in credentials
    python scripts/probe_angel.py
