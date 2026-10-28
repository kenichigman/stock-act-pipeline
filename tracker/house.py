"""House Clerk fetcher + PTR parser.

Index: https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip
  (XML listing every filing; FilingType 'P' = Periodic Transaction Report)
PTR PDF: https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{DocID}.pdf

The disclosure_date of every House record is the index FilingDate —
the day the report became public.
PDF tables are reconstructed by y-coordinate with pdfminer layout
objects because plain text extraction scrambles the columns.
"""
from __future__ import annotations

import io
import re
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

from .models import RecordRejected, DisclosureRecord
from .normalize import (clean_asset_name, extract_ticker,
                        map_transaction_type, parse_amount_range, parse_date)

BASE = "https://disclosures-clerk.house.gov/public_disc"
UA = {"User-Agent": "stock-act-pipeline/0.1 (public-data research; polite rate)"}


@dataclass
class HouseFiling:
    politician: str
    filing_date: str  # ISO
    doc_id: str
    year: int

    @property
    def pdf_url(self) -> str:
        return f"{BASE}/ptr-pdfs/{self.year}/{self.doc_id}.pdf"


def parse_index_xml(xml_bytes: bytes, year: int) -> list[HouseFiling]:
    root = ET.fromstring(xml_bytes.decode("utf-8-sig", "replace"))
    out = []
    for m in root.findall(".//Member"):
        d = {c.tag: (c.text or "").strip() for c in m}
        if d.get("FilingType") != "P":
            continue
        try:
            fdate = parse_date(d.get("FilingDate", ""))
        except ValueError:
            continue
        name = f"{d.get('First', '')} {d.get('Last', '')}".strip()
        out.append(HouseFiling(politician=name, filing_date=fdate,
                               doc_id=d.get("DocID", ""), year=year))
    out.sort(key=lambda f: f.filing_date, reverse=True)
    return out


def fetch_index(year: int, session: requests.Session | None = None) -> list[HouseFiling]:
    s = session or requests.Session()
    r = s.get(f"{BASE}/financial-pdfs/{year}FD.zip", headers=UA, timeout=60)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    xml_name = [n for n in z.namelist() if n.endswith(".xml")][0]
    return parse_index_xml(z.read(xml_name), year)


def pdf_rows(pdf_bytes: bytes) -> list[str]:
    """Reconstruct visual table rows from a PTR PDF: group text lines
    by y-coordinate per page, order by x. Returns row strings."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LAParams, LTTextLine

    rows: list[str] = []
    # all_texts=True: some House e-filing PDFs place the whole table
    # inside form XObjects (LTFigure); without it those pages yield
    # characters but no text lines at all.
    for page in extract_pages(io.BytesIO(pdf_bytes),
                              laparams=LAParams(all_texts=True)):
        lines: list[tuple[float, float, str]] = []
        stack = [page]
        while stack:
            el = stack.pop()
            if isinstance(el, LTTextLine):
                txt = el.get_text().replace("\x00", "").strip()
                if txt:
                    lines.append((el.y0, el.x0, txt))
            elif hasattr(el, "__iter__"):
                stack.extend(list(el))
        lines.sort(key=lambda t: (-t[0], t[1]))
        cur_y: float | None = None
        cur: list[tuple[float, str]] = []
        for y0, x0, txt in lines:
            if cur_y is None or abs(y0 - cur_y) <= 3.0:
                cur.append((x0, txt))
                cur_y = y0 if cur_y is None else cur_y
            else:
                rows.append(" ".join(t for _, t in sorted(cur)))
                cur = [(x0, txt)]
                cur_y = y0
        if cur:
            rows.append(" ".join(t for _, t in sorted(cur)))
    return rows


_SIMPLE_RE = re.compile(
    r"(?P<asset>.+?)\s+(?P<type>[PSE])(?:\s*\(partial\))?\s+"
    r"(?P<tdate>\d{2}/\d{2}/\d{4})\s+(?P<ndate>\d{2}/\d{2}/\d{4})\s+"
    # An asset name's own date (e.g. a T-bill maturity) can wrap into
    # the middle of the amount range: "$15,001 - 12/10/2026 $50,000".
    r"(?P<amount>\$[\d,]+\s*-\s*(?:\d{2}/\d{2}/\d{4}\s*)?\$[\d,]+"
    r"|Over\s*\$[\d,]+)")
_BRACKET_RE = re.compile(r"\[[A-Z]{2}\]")
_PAREN_RE = re.compile(r"\([A-Z][A-Z0-9.\-]{0,9}\)")


def _normalize_fragment(text: str) -> str:
    """Remove the [ST]-style asset-class brackets and the ticker
    parenthetical (captured separately) so the remaining text is in
    canonical order: ASSET TYPE DATE DATE AMOUNT — regardless of how
    the PDF wrapped the asset cell."""
    t = _BRACKET_RE.sub(" ", text)
    t = _PAREN_RE.sub(" ", t, count=1)
    return re.sub(r"\s+", " ", t).strip()


def merge_row_fragments(rows: list[str]) -> list[str]:
    """Merge the layout reconstructor's fragments into logical
    transaction strings. A visual row can split across two fragments
    (asset wrap puts the ticker + amount tail on the next line);
    filing-status and page-header fragments terminate a buffer."""
    out: list[str] = []
    buf: list[str] = []

    def flush():
        if buf:
            joined = " ".join(buf)
            if _SIMPLE_RE.search(_normalize_fragment(joined)):
                out.append(joined)
            buf.clear()

    for row in rows:
        stripped = row.strip()
        is_status = stripped.startswith("F") and "S:" in stripped
        is_header = "Notification" in stripped or stripped == "$200?"
        if is_status or is_header:
            flush()
            continue
        if stripped.startswith("S O:"):
            # Sub-owner annotation of the PREVIOUS transaction
            # (e.g. "S O: UBS IRA Account") — attach, never start a
            # buffer with it.
            flush()
            if out:
                out[-1] = out[-1] + " " + stripped
            continue
        is_continuation = stripped.startswith("(") or stripped.startswith("[")
        if buf and not is_continuation and _SIMPLE_RE.search(
                _normalize_fragment(" ".join(buf))):
            # The buffered transaction is complete and this fragment
            # begins a new one. (A complete-looking buffer must NOT
            # flush before its wrap continuation — "(TICKER) [ST]" —
            # has had its chance to attach.)
            flush()
        buf.append(row)
        if len(buf) > 4:  # runaway guard: never merge across trades
            flush()
    flush()
    return out


_SUBOWNER_RE = re.compile(r"S\s*O:\s*(.+)$")


def parse_house_row(row: str, filing: HouseFiling):
    """One logical transaction string -> DisclosureRecord, or None if it
    does not carry a complete transaction. Records with an asset but
    no ticker keep ticker=None."""
    sub_owner = ""
    m_so = _SUBOWNER_RE.search(row)
    if m_so:
        sub_owner = m_so.group(1).strip()
        row = row[:m_so.start()].strip()
    ticker = extract_ticker(row)
    m = _SIMPLE_RE.search(_normalize_fragment(row))
    if not m:
        return None
    gd = m.groupdict()
    ttype = map_transaction_type(gd["type"])
    # Drop a stray in-amount date (asset maturity that wrapped in).
    amount_text = re.sub(r"(\$[\d,]+\s*-\s*)\d{2}/\d{2}/\d{4}\s*",
                         r"\1", gd["amount"])
    label, low, high = parse_amount_range(amount_text)
    if ttype is None or label is None:
        return None
    try:
        tdate = parse_date(gd["tdate"])
    except ValueError:
        return None
    asset = clean_asset_name(gd["asset"])
    try:
        return DisclosureRecord(
            politician=filing.politician, chamber="house", ticker=ticker,
            transaction_type=ttype, amount_range=label, amount_low=low,
            amount_high=high, transaction_date=tdate,
            disclosure_date=filing.filing_date, source_url=filing.pdf_url,
            asset=asset, sub_owner=sub_owner)
    except RecordRejected:
        return None


def fetch_filing_records(filing: HouseFiling,
                         session: requests.Session | None = None):
    """Download one PTR PDF and parse it. Returns (records, n_rows_seen)."""
    s = session or requests.Session()
    r = s.get(filing.pdf_url, headers=UA, timeout=60)
    r.raise_for_status()
    rows = pdf_rows(r.content)
    logical = merge_row_fragments(rows)
    records = [rec for row in logical
               if (rec := parse_house_row(row, filing)) is not None]
    return records, len(rows)


def pull_house(year: int, limit: int, pause_s: float = 0.5,
               session: requests.Session | None = None):
    """Pull the `limit` most recent PTR filings. Returns (records, stats)."""
    s = session or requests.Session()
    filings = fetch_index(year, s)[:limit]
    records: list[DisclosureRecord] = []
    stats = {"filings": len(filings), "rows_seen": 0, "parsed": 0}
    for f in filings:
        recs, n = fetch_filing_records(f, s)
        stats["rows_seen"] += n
        stats["parsed"] += len(recs)
        records.extend(recs)
        time.sleep(pause_s)
    return records, stats
