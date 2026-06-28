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


class AuditVenueProductPageIntegration(unittest.TestCase):
    """Committed integration test: audit_venue() processes a product_pages
    entry via an injected extract_and_download, inspects the saved CSV, and
    yields an ACCEPT_FALLBACK attempt carrying the product-page metadata."""

    def test_audit_venue_uses_product_page_extraction(self):
        import tools.eu_source_audit.audit as A

        out = tempfile.mkdtemp()
        # write a synthetic 20-row SMI CSV to disk; the injected extractor
        # will "return" it as a saved file.
        csv_path = Path(out) / "smi_pp0_synthetic.csv"
        csv_path.write_bytes(_csv_bytes(20))

        page = ("https://www.ishares.com/ch/individual/en/products/251882/"
                "ishares-smi-ch")

        def fake_extract(page_url, outdir, venue, idx=0, timeout=30,
                         _fetch_fn=None):
            return {
                "product_page_url": page_url,
                "page_http_status": 200,
                "extracted_holdings_url": "https://x/fund/9.ajax?fileType=csv"
                                          "&fileName=CSSMI_holdings&"
                                          "dataType=fund",
                "holdings_http_status": 200,
                "saved_file": str(csv_path),
                "sha256": "deadbeef",
                "bytes": csv_path.stat().st_size,
                "status": "OK",
                "candidates": [],
            }

        # endpoints all fail so only the product page yields a result
        orig_extract = A.extract_and_download
        orig_dl = A.try_download
        try:
            A.extract_and_download = fake_extract
            A.try_download = lambda url, timeout=30: (None, b"ERR", False)
            vmeta = {
                "index": "SMI", "exchange": "SIX", "suffix": ".SW",
                "expected": 20,
                "equity_filter": {
                    "asset_class": ("aktien", "equity"),
                    "exchange_substr": ("six", "swiss"),
                    "currency": ("CHF",),
                },
                "endpoints": [("official_index", "http://dyn", "x")],
                "product_pages": [("reputable_etf_fallback", page, "iShares")],
            }
            r = A.audit_venue("smi", vmeta, out)
        finally:
            A.extract_and_download = orig_extract
            A.try_download = orig_dl

        # verdict and the product-page attempt
        self.assertEqual(r["verdict"], "ACCEPT_FALLBACK")
        pp = [a for a in r["attempts"]
              if a.get("via") == "product_page_extraction"]
        self.assertEqual(len(pp), 1)
        a = pp[0]
        self.assertEqual(a["extract_status"], "OK")
        self.assertEqual(a["product_page_url"], page)
        self.assertEqual(a["page_http_status"], 200)
        self.assertEqual(a["holdings_http_status"], 200)
        self.assertIn("fileType=csv", a["extracted_holdings_url"])
        self.assertIsNotNone(a["inspection"])
        self.assertEqual(len(a["inspection"]["included"]), 20)
        self.assertTrue(a["recommendation"].startswith("ACCEPT_FALLBACK"))

    def test_audit_venue_reports_failed_extraction_status(self):
        import tools.eu_source_audit.audit as A

        out = tempfile.mkdtemp()
        page = "https://www.ishares.com/ch/x/no-link"

        def fake_extract(page_url, outdir, venue, idx=0, timeout=30,
                         _fetch_fn=None):
            return {
                "product_page_url": page_url, "page_http_status": 200,
                "extracted_holdings_url": None, "holdings_http_status": None,
                "saved_file": None, "sha256": None, "bytes": 0,
                "status": "NO_HOLDINGS_LINK", "candidates": [],
            }

        orig_extract = A.extract_and_download
        orig_dl = A.try_download
        try:
            A.extract_and_download = fake_extract
            A.try_download = lambda url, timeout=30: (None, b"ERR", False)
            vmeta = {
                "index": "SMI", "exchange": "SIX", "suffix": ".SW",
                "expected": 20,
                "equity_filter": {"asset_class": ("equity",),
                                  "exchange_substr": ("six",),
                                  "currency": ("CHF",)},
                "endpoints": [("official_index", "http://dyn", "x")],
                "product_pages": [("reputable_etf_fallback", page, "iShares")],
            }
            r = A.audit_venue("smi", vmeta, out)
        finally:
            A.extract_and_download = orig_extract
            A.try_download = orig_dl

        pp = [a for a in r["attempts"]
              if a.get("via") == "product_page_extraction"][0]
        self.assertEqual(pp["extract_status"], "NO_HOLDINGS_LINK")
        self.assertEqual(pp["recommendation"], "NO_HOLDINGS_LINK")
        self.assertIsNone(pp["inspection"])
        # no usable fallback at all -> not ACCEPT
        self.assertNotEqual(r["verdict"], "ACCEPT_FALLBACK")


if __name__ == "__main__":
    unittest.main()
