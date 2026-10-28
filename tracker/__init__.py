"""STOCK Act disclosure pipeline.

Primary sources ONLY: House Clerk financial disclosures and Senate eFD.
Aggregator sites are never used as a source (House/Senate Stock Watcher
has been dead since 2021 — S3 403, stale data; others are cross-checks
at most, never primary).

The completeness rule: every record carries transaction_date AND
disclosure_date; a record missing either is rejected, never guessed.
"""

__version__ = "0.1.0"
