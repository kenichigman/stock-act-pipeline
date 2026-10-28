"""Storage: SQLite + JSONL export + the lag-distribution report
(median/p90 disclosure lag is a standing quality metric of the dataset)."""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

from .models import DisclosureRecord

_SCHEMA = """
CREATE TABLE IF NOT EXISTS disclosures (
    dedupe_key TEXT PRIMARY KEY,
    politician TEXT NOT NULL,
    chamber TEXT NOT NULL,
    ticker TEXT,
    asset TEXT,
    sub_owner TEXT,
    transaction_type TEXT NOT NULL,
    amount_range TEXT NOT NULL,
    amount_low INTEGER,
    amount_high INTEGER,
    transaction_date TEXT NOT NULL,
    disclosure_date TEXT NOT NULL,
    lag_days INTEGER NOT NULL,
    source_url TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute(_SCHEMA)
    return conn


def insert_records(conn: sqlite3.Connection,
                   records: list[DisclosureRecord]) -> int:
    """Insert, ignoring duplicates by dedupe_key. Returns new rows."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO disclosures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(r.dedupe_key, r.politician, r.chamber, r.ticker, r.asset,
          r.sub_owner, r.transaction_type, r.amount_range, r.amount_low,
          r.amount_high, r.transaction_date, r.disclosure_date,
          r.lag_days, r.source_url)
         for r in records])
    conn.commit()
    return conn.total_changes - before


def export_jsonl(conn: sqlite3.Connection, out_path: str | Path) -> int:
    rows = conn.execute(
        "SELECT politician, chamber, ticker, asset, sub_owner, "
        "transaction_type, amount_range, amount_low, amount_high, "
        "transaction_date, disclosure_date, lag_days, source_url "
        "FROM disclosures ORDER BY disclosure_date, politician, ticker").fetchall()
    cols = ["politician", "chamber", "ticker", "asset", "sub_owner",
            "transaction_type", "amount_range", "amount_low", "amount_high",
            "transaction_date", "disclosure_date", "lag_days", "source_url"]
    with open(out_path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(dict(zip(cols, row)), sort_keys=True) + "\n")
    return len(rows)


def _percentile(sorted_vals: list[int], pct: float) -> int:
    """Nearest-rank percentile."""
    if not sorted_vals:
        return 0
    rank = math.ceil(pct / 100.0 * len(sorted_vals))
    return sorted_vals[max(0, min(rank, len(sorted_vals)) - 1)]


def lag_report(records: list[DisclosureRecord]) -> dict:
    lags = sorted(r.lag_days for r in records)
    if not lags:
        return {"count": 0, "median_lag_days": None, "p90_lag_days": None,
                "min_lag_days": None, "max_lag_days": None}
    return {
        "count": len(lags),
        "median_lag_days": _percentile(lags, 50),
        "p90_lag_days": _percentile(lags, 90),
        "min_lag_days": lags[0],
        "max_lag_days": lags[-1],
    }
