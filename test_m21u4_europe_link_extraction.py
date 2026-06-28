"""M21.U4 Europe holdings-link extractor tests (read-only, no network).

Uses synthetic HTML + an injected fetch function to verify link extraction and
the distinct failure-mode statuses. No real network calls.
"""
import tempfile
import unittest
from pathlib import Path

from tools.eu_source_audit import holdings_link_extractor as E

_PAGE = "https://www.ishares.com/ch/individual/en/products/251882/ishares-smi"


def _csv_bytes(n_rows=20):
    lines = ["Ticker;Name;Asset Class;Exchange;Market Currency"]
    for i in range(n_rows):
        lines.append("T%02d;Co %d;Equity;SIX Swiss Exchange;CHF" % (i, i))
    return ("\n".join(lines)).encode()


def _html(*hrefs):
    body = "".join('<a href="%s">download</a>' % h for h in hrefs)
    return ("<!DOCTYPE html><html><head></head><body>%s</body></html>"
            % body).encode()


class ExtractLinks(unittest.TestCase):
    def test_extracts_relative_holdings_link(self):
        rel = ("/ch/individual/en/products/251882/fund/123.ajax"
               "?fileType=csv&fileName=CSSMI_holdings&dataType=fund")
        links = E.extract_links_from_html(_html(rel).decode(), _PAGE)
        self.assertEqual(len(links), 1)
        self.assertTrue(links[0].startswith("https://www.ishares.com/"))
        self.assertIn("fileType=csv", links[0])

    def test_extracts_absolute_holdings_link(self):
        absu = ("https://www.ishares.com/ch/x/fund/9.ajax"
                "?fileType=csv&fileName=CSSMI_holdings&dataType=fund")
        links = E.extract_links_from_html(_html(absu).decode(), _PAGE)
        self.assertEqual(links[0], absu)

    def test_rejects_html_with_no_csv_link(self):
        links = E.extract_links_from_html(
            _html("/about", "/contact", "/fund/overview.pdf").decode(), _PAGE)
        self.assertEqual(links, [])

    def test_multiple_links_prefers_holdings_filename(self):
        other = ("/x/fund/1.ajax?fileType=csv&fileName=characteristics&"
                 "dataType=fund")
        holdings = ("/x/fund/2.ajax?fileType=csv&fileName=CSSMI_holdings&"
                    "dataType=fund")
        links = E.extract_links_from_html(_html(other, holdings).decode(),
                                          _PAGE)
        self.assertIn("holdings", links[0].lower())

    def test_handles_amp_encoded_hrefs(self):
        rel = ("/x/fund/3.ajax?fileType=csv&amp;fileName=CSSMI_holdings&amp;"
               "dataType=fund")
        links = E.extract_links_from_html(_html(rel).decode(), _PAGE)
        self.assertEqual(len(links), 1)
        self.assertNotIn("&amp;", links[0])


class ExtractAndDownload(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()

    def _fetch_factory(self, page_resp, holdings_resp):
        calls = {"n": 0}

        def fetch(url, timeout=30):
            if url == _PAGE:
                return page_resp
            return holdings_resp
        return fetch

    def test_ok_when_page_has_link_and_holdings_download(self):
        rel = ("/x/fund/9.ajax?fileType=csv&fileName=CSSMI_holdings&"
               "dataType=fund")
        fetch = self._fetch_factory(
            (200, _html(rel), True), (200, _csv_bytes(20), True))
        rec = E.extract_and_download(_PAGE, self.out, "smi", _fetch_fn=fetch)
        self.assertEqual(rec["status"], "OK")
        self.assertTrue(rec["saved_file"])
        self.assertTrue(Path(rec["saved_file"]).is_file())
        self.assertEqual(rec["bytes"], len(_csv_bytes(20)))
        self.assertTrue(rec["sha256"])

    def test_page_unreachable(self):
        fetch = self._fetch_factory(
            (None, b"ERR:timeout", False), (200, _csv_bytes(), True))
        rec = E.extract_and_download(_PAGE, self.out, "smi", _fetch_fn=fetch)
        self.assertEqual(rec["status"], "PAGE_UNREACHABLE")
        self.assertIsNone(rec["saved_file"])

    def test_no_holdings_link(self):
        fetch = self._fetch_factory(
            (200, _html("/about", "/contact"), True),
            (200, _csv_bytes(), True))
        rec = E.extract_and_download(_PAGE, self.out, "smi", _fetch_fn=fetch)
        self.assertEqual(rec["status"], "NO_HOLDINGS_LINK")

    def test_holdings_link_unreachable(self):
        rel = "/x/fund/9.ajax?fileType=csv&fileName=h_holdings&dataType=fund"
        fetch = self._fetch_factory(
            (200, _html(rel), True), (404, b"ERR", False))
        rec = E.extract_and_download(_PAGE, self.out, "smi", _fetch_fn=fetch)
        self.assertEqual(rec["status"], "HOLDINGS_LINK_UNREACHABLE")
        self.assertIsNone(rec["saved_file"])

    def test_holdings_not_a_file(self):
        rel = "/x/fund/9.ajax?fileType=csv&fileName=h_holdings&dataType=fund"
        fetch = self._fetch_factory(
            (200, _html(rel), True),
            (200, b"<!DOCTYPE html><html>error</html>", True))
        rec = E.extract_and_download(_PAGE, self.out, "smi", _fetch_fn=fetch)
        self.assertEqual(rec["status"], "HOLDINGS_NOT_A_FILE")
        self.assertIsNone(rec["saved_file"])


if __name__ == "__main__":
    unittest.main()
