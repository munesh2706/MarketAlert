from datetime import datetime

import pytest

from marketalert.config import IST, load_config


@pytest.fixture
def cfg():
    return load_config()


def ist(y, mo, d, h=0, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


def opt_rows(expiry, strikes):
    return [{"token": f"{expiry}-{s:g}-{t}", "symbol": "X", "expiry": expiry, "strike": float(s), "type": t}
            for s in strikes for t in ("CE", "PE")]


@pytest.fixture
def filtered():
    """Tiny instruments_filtered.json content."""
    bnf = list(range(54800, 55300, 100))
    return {"date": "2026-10-06", "master_rows": 0, "skipped_option_rows": 0, "spot": {},
            "options": {
                "NIFTY": opt_rows("2026-10-06", range(22500, 22900, 50)) + opt_rows("2026-10-13", range(22500, 22900, 50)),
                "BANKNIFTY": opt_rows("2026-10-27", bnf) + opt_rows("2026-11-23", bnf),
                "SENSEX": opt_rows("2026-10-08", range(72500, 73100, 100)),
            }}
