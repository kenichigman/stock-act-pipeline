"""CLI: pull a sample (or a full recent window) from both chambers.

Usage:
  python -m tracker.cli pull --year 2026 --house-limit 8 \
      --senate-days 45 --senate-limit 8 --out sample

Writes into --out: disclosures.jsonl, disclosures.db, lag_report.json,
pull_log.json (per-source counts and any upstream block, verbatim).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import house, senate, store


def cmd_pull(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log: dict = {}

    house_records, hstats = house.pull_house(args.year, args.house_limit)
    log["house"] = hstats
    senate_records, sstats = senate.pull_senate(args.senate_days,
                                                args.senate_limit)
    log["senate"] = sstats

    records = house_records + senate_records
    conn = store.connect(out / "disclosures.db")
    inserted = store.insert_records(conn, records)
    exported = store.export_jsonl(conn, out / "disclosures.jsonl")
    conn.close()
    report = store.lag_report(records)
    (out / "lag_report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8")
    log["inserted_new"] = inserted
    log["exported_total"] = exported
    log["lag_report"] = report
    (out / "pull_log.json").write_text(
        json.dumps(log, indent=2), encoding="utf-8")
    print(json.dumps(log, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="tracker")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull")
    p.add_argument("--year", type=int, default=2026)
    p.add_argument("--house-limit", type=int, default=8)
    p.add_argument("--senate-days", type=int, default=45)
    p.add_argument("--senate-limit", type=int, default=8)
    p.add_argument("--out", default="sample")
    p.set_defaults(fn=cmd_pull)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
