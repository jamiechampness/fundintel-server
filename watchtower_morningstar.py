"""Watchtower - Morningstar documents-page reader (POC).
Reads the 'Documents' table on
  https://global.morningstar.com/en-gb/investments/funds/<MSID>/documents
Rows look like:  <Document Type> | Download | <Language> | <Effective Date>
Parsing is separate from fetching so it can be tested offline."""
import re
from datetime import datetime

# Morningstar label -> Watchtower doc type
LABELS = {
    "KIID": "KIID",
    "PRIIP KID": "PRIIP_KID",
    "Annual Report": "REPORT_ANNUAL",
    "Semi-Annual Report": "REPORT_INTERIM",
    "Factsheet": "FACTSHEET",
    "Prospectus": "PROSPECTUS",
}
_DATE = r"(\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4})"


def _parse(d):
    d = d.replace("Sept", "Sep").replace(".", "")
    for fmt in ("%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(d, fmt).date()
        except ValueError:
            pass
    return None


def parse_documents_text(text):
    """text = visible text of the page. Returns {doc_type: date or None}.
    Doc types listed on the page but with no parsable date come back as None
    (=> present but undated); types absent from the page are simply missing."""
    out = {}
    for label, dtype in LABELS.items():
        m = re.search(r"(?m)^\s*" + re.escape(label) + r"\b[^\n]*?" + _DATE, text)
        if m:
            out.setdefault(dtype, _parse(m.group(1)))
    return out


def fetch_documents(msid, timeout_ms=45000):
    """Needs Playwright (as on the Railway server). UNTESTED here - this
    workspace cannot reach Morningstar."""
    from playwright.sync_api import sync_playwright
    url = "https://global.morningstar.com/en-gb/investments/funds/%s/documents" % msid
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page()
        pg.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        pg.wait_for_selector("text=Effective Date", timeout=timeout_ms)
        text = pg.inner_text("body")
        b.close()
    return parse_documents_text(text)


if __name__ == "__main__":  # offline test with text transcribed from the screenshot
    sample = """Documents
Document Type Attachment Language Effective Date
Offering Documents
KIID
Download
English
18 Feb 2026
"""
    flat = """KIID Download English 18 Feb 2026
PRIIP KID Download English 18 Feb 2025
Prospectus Download English 10 Aug 2026
Annual Report Download English 30 Nov 2020
Semi-Annual Report Download English 31 May 2025
Factsheet Download English 30 Sept 2022"""
    for k, v in parse_documents_text(flat).items():
        print(k, v)
