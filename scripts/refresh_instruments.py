"""Refresh data/instruments_filtered.json (separate short-lived process; AGENTS.md §14).

Streams the scrip master to disk, parses it object by object and writes the filtered file
atomically. Exit code 0 on success, 1 on failure.

Usage: python scripts/refresh_instruments.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from marketalert import instruments as ins  # noqa: E402
from marketalert.config import ROOT, load_config, today_ist  # noqa: E402


def main() -> int:
    cfg = load_config()
    icfg = cfg["instruments"]
    master, filtered = ROOT / icfg["master_file"], ROOT / icfg["filtered_file"]
    master.parent.mkdir(parents=True, exist_ok=True)
    try:
        dl = ins.download_master(icfg["master_url"], master, icfg["download_timeout_seconds"],
                                 icfg["download_chunk_bytes"])
        t = time.monotonic()
        data = ins.build_filtered(master, cfg["indices"], icfg["strike_divisor"], today_ist(),
                                  icfg["parse_chunk_chars"])
        counts = {k: len(v) for k, v in data["options"].items()}
        if not all(counts.values()):
            raise RuntimeError(f"missing options for some index: {counts}")
        ins.write_json_atomic(filtered, data)
    except Exception as e:  # noqa: BLE001
        print(f"refresh failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"refreshed {data['date']}: download {dl['seconds']}s {dl['mb']}MB, "
          f"parse {time.monotonic() - t:.1f}s, options {counts}, skipped {data['skipped_option_rows']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
