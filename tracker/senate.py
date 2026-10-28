"""Senate eFD fetcher + PTR parser (efdsearch.senate.gov).

Access flow, as a normal client performs it: GET /search/ -> POST the
prohibition agreement -> land on /search/home/ -> POST the agreement
again -> the search UI unlocks at /search/. Search results come from
the DataTables endpoint /search/report/data/ (serverSide POST).
PTR report type id = 11. Electronic PTRs render as HTML tables at
/search/view/ptr/<uuid>/.

The disclosure_date of every Senate record is the report's
Date Received (filing date) from the search results — the first
date the disclosure is public.

KNOWN STATE (observed during development): the data endpoint was
serving senate.gov's
"Site Under Maintenance" 503 page while the rest of the flow worked.
The client raises SenateUnavailable in that case; it is an upstream
condition, not a parse failure.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser

import requests

from .models import RecordRejected, DisclosureRecord
from .normalize import (extract_ticker, map_transaction_type,
                        parse_amount_range, parse_date)

BASE = "https://efdsearch.senate.gov"
UA = {"User-Agent": "stock-act-pipeline/0.1 (public-data research; polite rate)"}
PTR_REPORT_TYPE = 11


class SenateUnavailable(RuntimeError):
    pass


@dataclass
class SenateReport:
    politician: str
    filing_date: str  # ISO (Date Received)
    view_url: str


_CSRF_RE = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')
_HREF_RE = re.compile(r'href="([^"]+)"')
_TAG_RE = re.compile(r"<[^>]+>")
_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}/\d{4}\b")


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html)).strip()


def parse_search_row(row: list) -> SenateReport | None:
    """One DataTables row (list of cell strings, HTML inside) ->
    SenateReport. Defensive by design: the view link and a date are
    located wherever they sit in the row."""
    cells = [_strip_tags(str(c)) for c in row]
    raw = " ".join(str(c) for c in row)
    url = None
    for href in _HREF_RE.findall(raw):
        if "/search/view/" in href:
            url = href if href.startswith("http") else BASE + href
            break
    dates = _DATE_RE.findall(raw)
    if url is None or not dates:
        return None
    try:
        fdate = parse_date(dates[0])
    except ValueError:
        return None
    name = cells[0] if cells else ""
    return SenateReport(politician=name, filing_date=fdate, view_url=url)


class _TableParser(HTMLParser):
    """Collect every table's rows as lists of cell texts."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None


def parse_ptr_html(html: str, report: SenateReport) -> list[DisclosureRecord]:
    """Extract transaction records from an electronic PTR view page.
    A row qualifies when it has a parseable transaction date, an
    amount range, and a recognizable transaction type across its
    cells; anything else is ignored, never guessed."""
    parser = _TableParser()
    parser.feed(html)
    records: list[DisclosureRecord] = []
    for table in parser.tables:
        for cells in table:
            joined = " | ".join(cells)
            label, low, high = parse_amount_range(joined)
            if label is None:
                continue
            ttype = None
            tdate = None
            asset = None
            for cell in cells:
                tt = map_transaction_type(cell)
                if tt:
                    ttype = tt
                try:
                    tdate = parse_date(cell)
                except ValueError:
                    pass
                if "(" in cell and ")" in cell and asset is None:
                    asset = cell
            if asset is None and cells:
                asset = cells[1] if len(cells) > 1 else cells[0]
            if ttype is None or tdate is None or asset is None:
                continue
            try:
                records.append(DisclosureRecord(
                    politician=report.politician, chamber="senate",
                    ticker=extract_ticker(asset), transaction_type=ttype,
                    amount_range=label, amount_low=low, amount_high=high,
                    transaction_date=tdate,
                    disclosure_date=report.filing_date,
                    source_url=report.view_url, asset=asset))
            except RecordRejected:
                continue
    return records


class SenateClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(UA)
        self._agreed = False

    def _csrf(self, html: str) -> str:
        m = _CSRF_RE.search(html)
        if not m:
            raise SenateUnavailable("no CSRF token on eFD page")
        return m.group(1)

    def agree(self):
        """Perform the site's terms click-through (twice, as served)."""
        s = self.session
        r = s.get(BASE + "/search/", timeout=30)
        s.post(BASE + "/search/",
               data={"csrfmiddlewaretoken": self._csrf(r.text),
                     "prohibition_agreement": "1"},
               headers={"Referer": BASE + "/search/"}, timeout=30)
        r = s.get(BASE + "/search/home/", timeout=30)
        r = s.post(BASE + "/search/home/",
                   data={"csrfmiddlewaretoken": self._csrf(r.text),
                         "prohibition_agreement": "1"},
                   headers={"Referer": BASE + "/search/home/"}, timeout=30)
        if "/search/" not in r.url and r.status_code not in (200, 302):
            raise SenateUnavailable(f"agreement flow ended at {r.url}")
        self._agreed = True

    def search_reports(self, start: str, end: str,
                       report_type: int = PTR_REPORT_TYPE,
                       length: int = 100) -> list[SenateReport]:
        """start/end are MM/DD/YYYY (transaction-date window as the
        site defines it). Raises SenateUnavailable on the site's
        maintenance page."""
        if not self._agreed:
            self.agree()
        s = self.session
        csrf = s.cookies.get("csrftoken", "")
        payload: dict[str, str] = {}
        for i in range(5):
            payload[f"columns[{i}][data]"] = str(i)
            payload[f"columns[{i}][searchable]"] = "true"
            payload[f"columns[{i}][orderable]"] = "true"
            payload[f"columns[{i}][search][value]"] = ""
            payload[f"columns[{i}][search][regex]"] = "false"
        payload.update({
            "draw": "1", "start": "0", "length": str(length),
            "order[0][column]": "1", "order[0][dir]": "asc",
            "search[value]": "", "search[regex]": "false",
            "start_date": start, "end_date": end,
            "report_types": json.dumps([report_type]),
            "filer_types": "[]",
            "submitted_start_date": "", "submitted_end_date": "",
            "candidate_state": "", "senator_state": "", "office_id": "",
            "first_name": "", "last_name": "",
        })
        r = s.post(BASE + "/search/report/data/", data=payload,
                   headers={"Referer": BASE + "/search/",
                            "X-Requested-With": "XMLHttpRequest",
                            "X-CSRFToken": csrf}, timeout=60)
        if r.status_code != 200 or "Site Under Maintenance" in r.text:
            raise SenateUnavailable(
                f"eFD data endpoint unavailable (HTTP {r.status_code})")
        try:
            data = r.json().get("data") or []
        except ValueError as exc:
            raise SenateUnavailable("eFD data endpoint returned non-JSON") from exc
        out = []
        for row in data:
            rep = parse_search_row(row)
            if rep is not None:
                out.append(rep)
        return out

    def fetch_report_records(self, report: SenateReport) -> list[DisclosureRecord]:
        r = self.session.get(report.view_url, timeout=30)
        r.raise_for_status()
        return parse_ptr_html(r.text, report)


def pull_senate(days: int, limit: int, pause_s: float = 0.5):
    """Pull PTR reports from the last `days` days. Returns
    (records, stats); stats['status'] explains any upstream block."""
    import datetime
    client = SenateClient()
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    try:
        reports = client.search_reports(
            start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y"))
    except SenateUnavailable as exc:
        return [], {"status": f"unavailable: {exc}", "reports": 0, "parsed": 0}
    reports = reports[:limit]
    records: list[DisclosureRecord] = []
    for rep in reports:
        try:
            records.extend(client.fetch_report_records(rep))
        except requests.RequestException:
            continue
        time.sleep(pause_s)
    return records, {"status": "ok", "reports": len(reports),
                     "parsed": len(records)}
