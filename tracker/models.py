"""Disclosure record model + validation. The completeness rule lives
here so no fetcher can bypass it: missing transaction_date or
disclosure_date raises RecordRejected."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


class RecordRejected(ValueError):
    """A candidate record that must NOT become a disclosure record."""


@dataclass
class DisclosureRecord:
    politician: str
    chamber: str  # "house" | "senate"
    ticker: str | None  # null when the filing names no parseable ticker
    transaction_type: str  # "buy" | "sell" | "exchange"
    amount_range: str  # canonical label, e.g. "$1,001 - $15,000"
    amount_low: int | None
    amount_high: int | None  # None = open-ended ("Over $1,000,000")
    transaction_date: str  # ISO yyyy-mm-dd
    disclosure_date: str  # ISO yyyy-mm-dd (the day the report became public)
    lag_days: int = field(init=False)
    source_url: str = ""
    asset: str = ""  # asset name as filed (audit + dedupe fidelity)
    sub_owner: str = ""  # e.g. "UBS IRA Account" — distinguishes the
    # same trade reported for two accounts in one filing

    def __post_init__(self):
        if not self.transaction_date or not self.disclosure_date:
            raise RecordRejected(
                "record missing transaction_date or disclosure_date — "
                "rejected, never guessed")
        from datetime import date
        t = date.fromisoformat(self.transaction_date)
        d = date.fromisoformat(self.disclosure_date)
        self.lag_days = (d - t).days
        if self.lag_days < 0:
            raise RecordRejected("disclosure before transaction date")
        if self.transaction_type not in ("buy", "sell", "exchange"):
            raise RecordRejected(f"bad transaction_type {self.transaction_type!r}")
        if self.chamber not in ("house", "senate"):
            raise RecordRejected(f"bad chamber {self.chamber!r}")

    @property
    def dedupe_key(self) -> str:
        raw = "|".join([
            self.politician, self.chamber, self.ticker or "",
            self.asset, self.sub_owner,
            self.transaction_type, self.amount_range,
            self.transaction_date, self.disclosure_date, self.source_url,
        ])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {
            "politician": self.politician,
            "chamber": self.chamber,
            "ticker": self.ticker,
            "asset": self.asset,
            "sub_owner": self.sub_owner,
            "transaction_type": self.transaction_type,
            "amount_range": self.amount_range,
            "amount_low": self.amount_low,
            "amount_high": self.amount_high,
            "transaction_date": self.transaction_date,
            "disclosure_date": self.disclosure_date,
            "lag_days": self.lag_days,
            "source_url": self.source_url,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)
