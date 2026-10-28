"""Normalization helpers shared by both chambers' parsers."""
from __future__ import annotations

import re
from datetime import date, datetime

_AMOUNT_RANGE_RE = re.compile(
    r"\$\s*([\d,]+)\s*-\s*\$\s*([\d,]+)")
_AMOUNT_OVER_RE = re.compile(r"Over\s*\$\s*([\d,]+)", re.IGNORECASE)
_TICKER_RE = re.compile(r"\(([A-Z][A-Z0-9.\-]{0,9})\)")


def parse_date(text: str) -> str:
    """'9/9/2026' / '09/09/2026' / ISO -> ISO yyyy-mm-dd. Raises ValueError."""
    text = (text or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unparseable date: {text!r}")


def parse_amount_range(text: str):
    """Return (canonical_label, low, high). high=None when open-ended.
    Returns (None, None, None) when no amount pattern is present."""
    text = (text or "").replace("\u2013", "-").replace("\u2014", "-")
    m = _AMOUNT_RANGE_RE.search(text)
    if m:
        low = int(m.group(1).replace(",", ""))
        high = int(m.group(2).replace(",", ""))
        return (f"${low:,} - ${high:,}", low, high)
    m = _AMOUNT_OVER_RE.search(text)
    if m:
        low = int(m.group(1).replace(",", ""))
        return (f"Over ${low:,}", low, None)
    return (None, None, None)


_TXN_MAP = {
    "P": "buy", "PURCHASE": "buy", "BUY": "buy",
    "S": "sell", "SALE": "sell", "SELL": "sell",
    "S (PARTIAL)": "sell", "SALE (PARTIAL)": "sell",
    "E": "exchange", "EXCHANGE": "exchange",
}


def map_transaction_type(raw: str) -> str | None:
    key = re.sub(r"\s+", " ", (raw or "").strip()).upper()
    if key in _TXN_MAP:
        return _TXN_MAP[key]
    if key.startswith("S") and "PARTIAL" in key:
        return "sell"
    return None


def extract_ticker(asset_text: str) -> str | None:
    """Ticker is the parenthesized symbol in the asset name, e.g.
    'Brown & Brown, Inc. Common Stock (BRO) [ST]' -> 'BRO'."""
    m = _TICKER_RE.search(asset_text or "")
    return m.group(1) if m else None


def clean_asset_name(asset_text: str) -> str:
    """Strip House PTR owner prefixes (SP/JT/DC) and filing-status
    fragments that pdfminer interleaves into row text."""
    t = re.sub(r"\s+", " ", (asset_text or "")).strip()
    t = re.sub(r"^(SP|JT|DC)\s+", "", t)
    t = re.sub(r"\bF\s+S:\s*New\b", "", t).strip()
    t = re.sub(r"\s+", " ", t)
    return t
