# stock-act-pipeline

Ingestion pipeline for U.S. congressional transaction disclosures
(STOCK Act Periodic Transaction Reports), built from the two primary
sources — the House Clerk's disclosure index and PDFs, and the Senate
eFD system — normalized into one record shape, deduplicated in SQLite,
with disclosure lag measured for every record.

A disclosure is stale by construction: the STOCK Act allows up to
45 days between a transaction and its public filing. This pipeline
treats that staleness as data, not as a footnote — every record carries
both dates, so the lag is measured, never hidden.

## Sources — primary only

- **House:** Clerk financial-disclosure index ZIP
  (`disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip`)
  + PTR PDFs (`ptr-pdfs/{year}/{DocID}.pdf`). The disclosure date is
  the index **FilingDate** — the day the report became public.
- **Senate:** eFD (`efdsearch.senate.gov`) — the terms agreement flow
  performed as a normal client (agreement POSTed twice, as the site
  serves it), DataTables search for report type 11 (PTR), PTR view
  pages parsed as HTML tables. Disclosure date = Date Received.
- **Never used as a source:** aggregator sites. House/Senate Stock
  Watcher has been dead since 2021 (S3 403, stale data); other
  aggregators are cross-checks at most, never primary.

## The completeness rule

`{politician, chamber, ticker|null, asset, sub_owner,
transaction_type (buy|sell|exchange), amount_range, amount_low,
amount_high, transaction_date, disclosure_date, lag_days, source_url}`,
with `lag_days = disclosure_date − transaction_date`.

**A record missing either date is rejected, never guessed**
(`models.RecordRejected`, raised in the record's own validation, so no
fetcher can bypass it). An undated disclosure cannot be placed in time,
and a pipeline that guesses dates manufactures precision it does not
have.

## How it works

- `tracker/house.py` — index + PDF fetch; pdfminer layout rows
  (LAParams `all_texts=True` — some e-filings put the table inside
  form XObjects), y-coordinate row reconstruction, fragment merging
  for wrapped asset cells, sub-owner attachment.
- `tracker/senate.py` — agreement flow, search, PTR HTML parsing.
- `tracker/normalize.py`, `tracker/models.py` — shared parsing + the
  record and its completeness rule.
- `tracker/store.py` — SQLite (dedupe by content hash including asset
  and sub-owner, so the same transaction reported for two accounts
  stays two records), JSONL export, lag-distribution report
  (nearest-rank median/p90).
- `tracker/cli.py` — the pull entry point.

## Usage

```bash
pip install -r requirements.txt
python -m tracker.cli pull --year 2026 --house-limit 8 \
    --senate-days 45 --senate-limit 8 --out sample
python -m unittest discover -s tests   # 20 tests, fixture-based
```

Writes into `--out`: `disclosures.jsonl`, `disclosures.db`,
`lag_report.json`, and `pull_log.json` (per-source counts and any
upstream block, recorded verbatim).

`tests/test_tracker.py` — 20 tests; House fixtures transcribed from
real PTRs (public filings: DocIDs 20035408, 20035562, 20035539).

## Sample output

One development pull of the 8 most recent House PTR filings produced
**53 records, lag median 13d / p90 33d (range 4–35d)** — see `sample/`
(`disclosures.jsonl`, `lag_report.json`, `pull_log.json`; the SQLite
database is regenerable via the CLI and not committed).

## Known limits

- **Senate live pull can be blocked upstream:** the eFD data endpoint
  has served senate.gov's "Site Under Maintenance" 503 page for whole
  sessions while the agreement/search flow itself worked. The client
  raises `SenateUnavailable` and the pull records the status verbatim
  in `pull_log.json`. Retry when the site is back; the parsers are
  fixture-tested meanwhile.
- **Paper-filed House PTRs** (DocIDs beginning 911…) are image-only
  scans — no text layer, so they yield zero records until/unless OCR
  is added. Electronic filings parse; a page-boundary asset wrap can
  still cost an occasional row (counted in `rows_seen` vs `parsed`).

## Status

Working pipeline, developed against the live primary sources; the
House path is exercised end-to-end, the Senate path is fixture-tested
against an intermittently unavailable upstream. Public data only —
every source document is a public filing.
