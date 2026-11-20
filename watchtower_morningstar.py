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
    Works whether cells are tab-separated on one line or one per line:
    find the label as its own cell, then take the first date in the next
    few cells (Attachment | Language | Effective Date)."""
    cells = [c.strip() for c in re.split(r"[\n\t]+", text) if c.strip()]
    out = {}
    for i, c in enumerate(cells):
        dtype = LABELS.get(c)
        if not dtype or dtype in out:
            continue
        for nxt in cells[i + 1:i + 7]:
            if nxt in LABELS:      # ran into the next row's label
                break
            m = re.search(_DATE, nxt)
            if m:
                out[dtype] = _parse(m.group(1))
                break
    return out


# Same browser identity as the working FundIntel scrapers (Linux Chrome)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")
_BLOCK = ("image", "media", "font")
_BLOCK_HOSTS = ("qualtrics", "doubleclick")


_msid_cache = {}
_NAV = ("https://global.morningstar.com/api/v1/en-gb/stores/search/nav"
        "?marketID=GBR&q=%s")


def _pick_msid(data, isin):
    """From the search response, the performanceID whose isin matches."""
    for e in (data or {}).get("entities", []):
        v = e.get("value") or {}
        if v.get("isin", "").upper() == isin.upper() and v.get("performanceID"):
            return v["performanceID"]
    return None


def isin_to_msid(isin, timeout_ms=45000):
    """ISIN -> Morningstar page id (0P...) via Morningstar's own search API
    (the call its search box makes). Plain request first; if Morningstar
    refuses it, retry from inside a browser session."""
    isin = isin.upper()
    if isin in _msid_cache:
        return _msid_cache[isin]
    msid, why = None, ""
    try:
        import requests
        r = requests.get(_NAV % isin, timeout=20, headers={
            "User-Agent": UA, "Accept": "application/json",
            "Accept-Language": "en-GB,en;q=0.9",
            "Referer": "https://global.morningstar.com/en-gb"})
        if r.status_code == 200:
            msid = _pick_msid(r.json(), isin)
            why = "plain request OK but no exact ISIN match"
        else:
            why = "plain request got HTTP %s" % r.status_code
    except Exception as e:
        why = "plain request failed: %s" % e
    if not msid:
        import json
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            br = p.chromium.launch(headless=True, args=[
                "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--disable-blink-features=AutomationControlled"])
            try:
                ctx = br.new_context(user_agent=UA, locale="en-GB",
                                     timezone_id="Europe/London")
                pg = ctx.new_page()
                pg.goto("https://global.morningstar.com/en-gb",
                        wait_until="domcontentloaded", timeout=timeout_ms)
                pg.wait_for_timeout(3000)
                # 1) same request, sent from inside the loaded Morningstar page
                res = pg.evaluate(
                    """async u => { const r = await fetch(u, {credentials:'include',
                         headers:{'Accept':'application/json'}});
                         return {s: r.status, t: (await r.text()).slice(0, 200000)}; }""",
                    _NAV % isin)
                try:
                    msid = _pick_msid(json.loads(res["t"]), isin)
                except Exception:
                    why += " | in-page fetch HTTP %s, body starts: %r" % (
                        res.get("s"), res.get("t", "")[:120])
                # 2) type into the real search box and read the page's own call
                if not msid:
                    box = pg.locator("input[type='search'], input[placeholder*='earch'],"
                                     " input[aria-label*='earch']").first
                    with pg.expect_response(
                            lambda r: "/stores/search/nav" in r.url and isin in r.url,
                            timeout=timeout_ms) as ri:
                        box.click()
                        box.type(isin, delay=60)
                    msid = _pick_msid(ri.value.json(), isin)
                    why += " | search-box route found no match" if not msid else ""
            except Exception as e:
                why += " | browser route failed: %s" % str(e)[:200]
            finally:
                br.close()
    if not msid:
        raise RuntimeError("Morningstar has no fund matching %s (%s)" % (isin, why))
    _msid_cache[isin] = msid
    return msid


def fetch_documents(msid, timeout_ms=45000):
    """Needs Playwright (as on the Railway server). Light-weight: blocks
    images, fonts, trackers so the page fits in a small container."""
    from playwright.sync_api import sync_playwright
    url = "https://global.morningstar.com/en-gb/investments/funds/%s/documents" % msid
    with sync_playwright() as p:
        br = p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
            "--disable-extensions",
            "--disable-blink-features=AutomationControlled"])
        try:
            ctx = br.new_context(
                user_agent=UA, locale="en-GB", timezone_id="Europe/London",
                viewport={"width": 1366, "height": 900},
                extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"})
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            pg = ctx.new_page()

            def gate(route):
                r = route.request
                if r.resource_type in _BLOCK or any(h in r.url for h in _BLOCK_HOSTS):
                    return route.abort()
                return route.continue_()
            pg.route("**/*", gate)
            pg.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                pg.wait_for_selector("text=Effective Date", timeout=timeout_ms)
            except Exception:
                # Report what the browser actually saw, to tell a bot-block
                # from a slow page or a changed layout.
                try:
                    body = " ".join(pg.inner_text("body").split())[:600]
                except Exception:
                    body = "(no body text)"
                raise RuntimeError("Documents table not seen. url=%s | title=%s | page says: %s"
                                   % (pg.url, pg.title(), body))
            text = pg.inner_text("body")
            docs = parse_documents_text(text)
            if not docs:
                i = text.find("Effective Date")
                raise RuntimeError("Table seen but no dates parsed. Raw sample: %r"
                                   % text[max(0, i - 50):i + 700])
            return docs
        finally:
            br.close()


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
