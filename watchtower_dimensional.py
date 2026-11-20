"""Watchtower - Dimensional (asset manager = SOURCE OF TRUTH) reader. POC.
Permission to automate access granted by Dimensional.

Flow: open fund page -> accept the professional-investor gate -> read the
document links -> download each PDF -> read the date the document states
about itself (first pages only). Dimensional's page shows NO dates, so the
PDF text is the only reliable source."""
import io
import re
from watchtower import extract_doc_date

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")

# Dimensional link label -> Watchtower doc type
WANTED = {
    "UCITS KIID - English": "KIID",
    "Fact Sheet": "FACTSHEET",
    "Annual Report & Accounts (November)": "REPORT_ANNUAL",
    "Interim Report & Accounts (May)": "REPORT_INTERIM",
}
_cache = {}   # pdf url -> date  (the two reports are shared by every fund)


def pdf_first_pages_text(data, pages=3):
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(data))
    return "\n".join((r.pages[i].extract_text() or "")
                     for i in range(min(pages, len(r.pages))))


def accept_gate(pg):
    """Tick the terms box and press 'Accept and continue' if shown."""
    try:
        pg.wait_for_selector("text=Accept and continue", timeout=8000)
    except Exception:
        return  # no gate this time
    try:
        pg.get_by_label(re.compile("read and agree", re.I)).check(timeout=4000)
    except Exception:
        pg.locator("input[type=checkbox]").first.check(timeout=4000)
    pg.get_by_text(re.compile("accept and continue", re.I)).first.click()


def fetch_source_dates(isin, timeout_ms=45000):
    """Returns {'dates': {doc_type: date|None}, 'urls': {doc_type: url}}."""
    from playwright.sync_api import sync_playwright
    url = "https://www.dimensional.com/gb-en/funds/%s" % isin.lower()
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            "--disable-blink-features=AutomationControlled"])
        try:
            ctx = br.new_context(user_agent=UA, locale="en-GB",
                                 timezone_id="Europe/London")
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            accept_gate(pg)
            try:
                pg.wait_for_selector(".tools-lens-documents-list-row-link",
                                     state="attached", timeout=timeout_ms)
            except Exception:
                body = " ".join(pg.inner_text("body").split())[:500]
                raise RuntimeError("Dimensional documents not seen. url=%s | "
                                   "title=%s | page says: %s"
                                   % (pg.url, pg.title(), body))
            links = pg.eval_on_selector_all(
                ".tools-lens-documents-list-row-link",
                """els => els.map(a => ({
                       name: (a.querySelector('.tools-lens-documents-list-doc-name')
                              || a).innerText.trim(),
                       href: a.href}))""")
            out = {"dates": {}, "urls": {}}
            for l in links:
                dtype = WANTED.get(l["name"])
                if not dtype or dtype in out["urls"]:
                    continue
                out["urls"][dtype] = l["href"]
                href = l["href"]
                if href not in _cache:
                    resp = ctx.request.get(href, timeout=timeout_ms)
                    if not resp.ok:
                        _cache[href] = None
                    else:
                        txt = pdf_first_pages_text(resp.body())
                        _cache[href] = extract_doc_date(txt, dtype)
                out["dates"][dtype] = _cache[href]
            for dtype in WANTED.values():      # listed on the page?
                out["dates"].setdefault(dtype, None)
            return out
        finally:
            br.close()
