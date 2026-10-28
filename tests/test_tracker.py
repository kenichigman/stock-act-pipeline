"""Parser/normalizer/store tests with fixtures. House row fixtures are
transcribed from a real PTR (Rep. Delaney, Filing ID 20035408) as the
layout reconstructor emits them."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tracker import house, senate, store  # noqa: E402
from tracker.models import RecordRejected, DisclosureRecord  # noqa: E402
from tracker.normalize import (extract_ticker, map_transaction_type,  # noqa: E402
                               parse_amount_range, parse_date)


class TestNormalize(unittest.TestCase):
    def test_parse_date(self):
        self.assertEqual(parse_date("9/9/2026"), "2026-09-09")
        self.assertEqual(parse_date("09/09/2026"), "2026-09-09")
        self.assertEqual(parse_date("2026-09-09"), "2026-09-09")
        with self.assertRaises(ValueError):
            parse_date("not a date")

    def test_amount_range(self):
        self.assertEqual(parse_amount_range("$1,001 - $15,000"),
                         ("$1,001 - $15,000", 1001, 15000))
        self.assertEqual(parse_amount_range("Over $1,000,000"),
                         ("Over $1,000,000", 1000000, None))
        self.assertEqual(parse_amount_range("no money here"),
                         (None, None, None))

    def test_ticker_and_type(self):
        self.assertEqual(
            extract_ticker("Brown & Brown, Inc. Common Stock (BRO) [ST]"),
            "BRO")
        self.assertIsNone(extract_ticker("Some Fund Without Ticker"))
        self.assertEqual(map_transaction_type("P"), "buy")
        self.assertEqual(map_transaction_type("S (partial)"), "sell")
        self.assertEqual(map_transaction_type("Sale"), "sell")
        self.assertEqual(map_transaction_type("Exchange"), "exchange")
        self.assertIsNone(map_transaction_type("??"))


class TestModel(unittest.TestCase):
    def _kw(self, **over):
        kw = dict(politician="X Y", chamber="house", ticker="ABC",
                  transaction_type="buy", amount_range="$1,001 - $15,000",
                  amount_low=1001, amount_high=15000,
                  transaction_date="2026-08-25", disclosure_date="2026-09-09",
                  source_url="https://example.invalid/x.pdf")
        kw.update(over)
        return kw

    def test_lag_computed(self):
        self.assertEqual(DisclosureRecord(**self._kw()).lag_days, 15)

    def test_missing_dates_rejected(self):
        with self.assertRaises(RecordRejected):
            DisclosureRecord(**self._kw(transaction_date=""))
        with self.assertRaises(RecordRejected):
            DisclosureRecord(**self._kw(disclosure_date=""))


FILING = house.HouseFiling(politician="April McClain Delaney",
                           filing_date="2026-09-09", doc_id="20035408",
                           year=2026)


class TestHouseRows(unittest.TestCase):
    def test_sale_row(self):
        rec = house.parse_house_row(
            "DC Brown & Brown, Inc. Common Stock (BRO) [ST] S "
            "08/25/2026 09/01/2026 $100,001 - $250,000", FILING)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.ticker, "BRO")
        self.assertEqual(rec.transaction_type, "sell")
        self.assertEqual(rec.transaction_date, "2026-08-25")
        self.assertEqual(rec.disclosure_date, "2026-09-09")
        self.assertEqual(rec.lag_days, 15)
        self.assertEqual(rec.amount_low, 100001)

    def test_buy_row(self):
        rec = house.parse_house_row(
            "DC BWX Technologies, Inc. Common Stock (BWXT) [ST] P "
            "08/03/2026 09/01/2026 $1,001 - $15,000", FILING)
        self.assertEqual(rec.transaction_type, "buy")
        self.assertEqual(rec.ticker, "BWXT")

    def test_partial_sale_row(self):
        rec = house.parse_house_row(
            "DC Corpay, Inc. Common Stock (CPAY) [ST] S (partial) "
            "08/06/2026 09/01/2026 $15,001 - $50,000", FILING)
        self.assertEqual(rec.transaction_type, "sell")
        self.assertEqual(rec.ticker, "CPAY")

    def test_no_ticker_row_kept_with_null_ticker(self):
        rec = house.parse_house_row(
            "DC Example Index Fund [MF] P 08/03/2026 09/01/2026 "
            "$1,001 - $15,000", FILING)
        self.assertIsNotNone(rec)
        self.assertIsNone(rec.ticker)

    def test_wrapped_row_fragments_merge(self):
        # Transcribed from the real layout output: the asset wrap puts
        # the ticker + amount tail on the next fragment.
        frags = [
            "DC Clean Harbors, Inc. Common Stock S 08/25/2026 "
            "09/01/2026 $50,001 -",
            "(CLH) [ST] $100,000",
            "F S: New",
            "DC Corpay, Inc. Common Stock (CPAY) S (partial) "
            "08/06/2026 09/01/2026 $15,001 -",
            "[ST] $50,000",
        ]
        logical = house.merge_row_fragments(frags)
        self.assertEqual(len(logical), 2)
        rec = house.parse_house_row(logical[0], FILING)
        self.assertEqual(rec.ticker, "CLH")
        self.assertEqual(rec.transaction_type, "sell")
        self.assertEqual(rec.amount_range, "$50,001 - $100,000")
        rec2 = house.parse_house_row(logical[1], FILING)
        self.assertEqual(rec2.ticker, "CPAY")
        self.assertEqual(rec2.transaction_type, "sell")

    def test_tbill_maturity_date_inside_amount(self):
        # Real case (Rep. Fong, Filing ID 20035539): the asset's own
        # maturity date wraps into the middle of the amount range.
        frags = [
            "U.S. Treasury Bill 0.0% Int Mat P 09/18/2026 "
            "09/18/2026 $15,001 -",
            "12/10/2026 [GS] $50,000",
        ]
        logical = house.merge_row_fragments(frags)
        self.assertEqual(len(logical), 1)
        fong = house.HouseFiling(politician="Vince Fong",
                                 filing_date="2026-10-05",
                                 doc_id="20035539", year=2026)
        rec = house.parse_house_row(logical[0], fong)
        self.assertIsNotNone(rec)
        self.assertEqual(rec.amount_range, "$15,001 - $50,000")
        self.assertIsNone(rec.ticker)

    def test_sub_owner_fragment_attaches(self):
        frags = [
            "Cisco Systems, Inc. - Common Stock S (partial) "
            "09/08/2026 10/01/2026 $1,001 - $15,000",
            "(CSCO) [ST]",
            "F S: New",
            "S O: UBS IRA Account",
        ]
        logical = house.merge_row_fragments(frags)
        self.assertEqual(len(logical), 1)
        rec = house.parse_house_row(logical[0], FILING)
        self.assertEqual(rec.ticker, "CSCO")
        self.assertEqual(rec.sub_owner, "UBS IRA Account")

    def test_header_row_skipped(self):
        self.assertIsNone(house.parse_house_row(
            "ID Owner Asset Transaction Type Date Notification Date "
            "Amount Cap. Gains > $200?", FILING))

    def test_index_xml_filters_ptrs(self):
        xml = b"""<?xml version="1.0"?><FinancialDisclosure>
        <Member><Last>Doe</Last><First>Jane</First><FilingType>P</FilingType>
        <StateDst>MD06</StateDst><Year>2026</Year>
        <FilingDate>9/9/2026</FilingDate><DocID>20035408</DocID></Member>
        <Member><Last>Roe</Last><First>John</First><FilingType>A</FilingType>
        <StateDst>TX01</StateDst><Year>2026</Year>
        <FilingDate>5/1/2026</FilingDate><DocID>999</DocID></Member>
        </FinancialDisclosure>"""
        filings = house.parse_index_xml(xml, 2026)
        self.assertEqual(len(filings), 1)
        self.assertEqual(filings[0].politician, "Jane Doe")
        self.assertEqual(filings[0].pdf_url,
                         "https://disclosures-clerk.house.gov/public_disc/"
                         "ptr-pdfs/2026/20035408.pdf")


SENATE_REPORT = senate.SenateReport(
    politician="Jane Doe", filing_date="2026-09-05",
    view_url="https://efdsearch.senate.gov/search/view/ptr/abc/")

SENATE_PTR_HTML = """
<html><body><table>
<tr><th>#</th><th>Asset</th><th>Transaction Type</th><th>Date</th>
<th>Amount</th></tr>
<tr><td>1</td><td>Apple Inc. (AAPL) [ST]</td><td>Purchase</td>
<td>08/14/2026</td><td>$1,001 - $15,000</td></tr>
<tr><td>2</td><td>Microsoft Corporation (MSFT) [ST]</td>
<td>Sale (Partial)</td><td>08/20/2026</td><td>$15,001 - $50,000</td></tr>
</table></body></html>"""


class TestSenate(unittest.TestCase):
    def test_search_row(self):
        row = ["Jane Doe", "Senator",
               '<a href="/search/view/ptr/abc/">Periodic Transaction '
               'Report</a>', "09/05/2026"]
        rep = senate.parse_search_row(row)
        self.assertEqual(rep.politician, "Jane Doe")
        self.assertEqual(rep.filing_date, "2026-09-05")
        self.assertTrue(rep.view_url.endswith("/search/view/ptr/abc/"))

    def test_search_row_without_link_or_date(self):
        self.assertIsNone(senate.parse_search_row(["Jane Doe", "Senator"]))

    def test_ptr_html(self):
        recs = senate.parse_ptr_html(SENATE_PTR_HTML, SENATE_REPORT)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0].ticker, "AAPL")
        self.assertEqual(recs[0].transaction_type, "buy")
        self.assertEqual(recs[0].lag_days, 22)  # 8/14 -> 9/05
        self.assertEqual(recs[1].transaction_type, "sell")
        self.assertEqual(recs[1].disclosure_date, "2026-09-05")


class TestStore(unittest.TestCase):
    def _rec(self, tdate, ddate, ticker="ABC", sub_owner=""):
        return DisclosureRecord(
            politician="X Y", chamber="house", ticker=ticker,
            transaction_type="buy", amount_range="$1,001 - $15,000",
            amount_low=1001, amount_high=15000, transaction_date=tdate,
            disclosure_date=ddate, source_url="https://example.invalid/x",
            sub_owner=sub_owner)

    def test_same_trade_two_accounts_not_deduped(self):
        with tempfile.TemporaryDirectory() as td:
            conn = store.connect(os.path.join(td, "s.db"))
            a = self._rec("2026-09-08", "2026-10-01", sub_owner="IRA")
            b = self._rec("2026-09-08", "2026-10-01", sub_owner="Brokerage")
            self.assertEqual(store.insert_records(conn, [a, b]), 2)
            conn.close()

    def test_insert_dedupes_and_exports(self):
        with tempfile.TemporaryDirectory() as td:
            conn = store.connect(os.path.join(td, "s.db"))
            rec = self._rec("2026-08-25", "2026-09-09")
            self.assertEqual(store.insert_records(conn, [rec, rec]), 1)
            out = os.path.join(td, "s.jsonl")
            self.assertEqual(store.export_jsonl(conn, out), 1)
            conn.close()  # Windows cannot delete an open SQLite file

    def test_lag_report_math(self):
        recs = [self._rec("2026-01-01", "2026-01-02"),   # lag 1
                self._rec("2026-01-01", "2026-01-03"),   # lag 2
                self._rec("2026-01-01", "2026-01-04"),   # lag 3
                self._rec("2026-01-01", "2026-01-05"),   # lag 4
                self._rec("2026-01-01", "2026-01-11")]   # lag 10
        rep = store.lag_report(recs)
        self.assertEqual(rep["count"], 5)
        self.assertEqual(rep["median_lag_days"], 3)   # nearest-rank 3rd
        self.assertEqual(rep["p90_lag_days"], 10)     # nearest-rank 5th
        self.assertEqual(store.lag_report([])["count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
