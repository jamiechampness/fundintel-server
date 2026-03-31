#!/usr/bin/env python3
"""
FundIntel Backend Server v11
Run with: python3 fundintel_server.py
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import re
import time
import json
import os

app = Flask(__name__)
CORS(app)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# Check if Playwright is available
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
    print("✓ Playwright available")
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("✗ Playwright not available - using requests fallback")

def get_sedol(isin):
    """Extract SEDOL from ISIN. GB ISINs: GB + 00 + SEDOL(7) + check(1)"""
    if isin.startswith('GB') and len(isin) == 12:
        return isin[4:11]
    return isin[2:9]

def to_pct(val):
    if val is None:
        return None
    try:
        f = float(str(val).strip().rstrip('%').replace(',', ''))
        return f"{f:.2f}%"
    except:
        return None

def normalise(data):
    if not data:
        return None
    out = {k: data.get(k) for k in ("ter", "entryCharge", "exitCharge", "perf1y", "srri", "nav")}
    return out if any(v is not None for v in out.values()) else None

def extract_ocf(text):
    """Find first realistic OCF percentage near relevant keywords."""
    for keyword in ['OCF/TER', 'Ongoing charge', 'Ongoing Charge', 'OCF', 'TER']:
        pos = text.find(keyword)
        if pos > -1:
            snippet = text[pos:pos+400]
            for m in re.finditer(r'([\d]+\.[\d]{1,3})', snippet):
                try:
                    f = float(m.group(1))
                    if 0.05 < f < 5.0:
                        return f"{f:.2f}%"
                except:
                    pass
    return None

def fetch_page_with_playwright(url, wait_for=None, click=None):
    """Fetch a page using Playwright and return the HTML content."""
    if not PLAYWRIGHT_AVAILABLE:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if wait_for:
                try:
                    page.wait_for_selector(wait_for, timeout=8000)
                except:
                    pass
            # Extra wait for JS rendering
            page.wait_for_timeout(2000)
            content = page.content()
            final_url = page.url
            browser.close()
            return content, final_url
    except Exception as e:
        print(f"  Playwright error: {e}")
        return None, None

# ── Hargreaves Lansdown ───────────────────────────────────────
def fetch_hl(isin):
    """
    HL: Use Playwright to render the JavaScript-heavy search page.
    The search redirects to fund page in a real browser.
    """
    try:
        sedol = get_sedol(isin)
        print(f"  HL SEDOL: {sedol}")

        search_url = f"https://www.hl.co.uk/funds/fund-discounts,-prices--and--factsheets/search-results?ISINsearch={isin}"

        if PLAYWRIGHT_AVAILABLE:
            print(f"  HL using Playwright...")
            content, final_url = fetch_page_with_playwright(search_url, wait_for='table')
            if content:
                print(f"  HL Playwright URL: {final_url}")
                # If still on search page, find and follow fund link
                if 'ISINsearch' in (final_url or ''):
                    m = re.search(r'href="(/funds/fund-discounts[^"]+/[a-z][a-z0-9\-]+)"', content, re.IGNORECASE)
                    if m:
                        path = m.group(1)
                        if not any(x in path for x in ['/invest', '/key-features', '/charts', '/research', '/costs', '/fund-analysis']):
                            fund_url = "https://www.hl.co.uk" + path
                            content, final_url = fetch_page_with_playwright(fund_url)
                            print(f"  HL fund page: {final_url}")
                text = content
            else:
                text = ""
        else:
            r = SESSION.get(search_url, timeout=15, allow_redirects=True)
            text = r.text

        ter = extract_ocf(text)
        entry_m = re.search(r'Net initial charge[^\d]*([\d.]+)%', text)
        entry = to_pct(entry_m.group(1)) if entry_m else None
        nav_m = re.search(r'Sell:([\d,]+\.?\d*p)', text)
        nav = nav_m.group(1) if nav_m else None
        perf_m = re.search(r'20/03/25 to 20/03/26[^0-9+\-]*([\+\-]?[\d.]+)%', text)
        perf = f"{perf_m.group(1)}%" if perf_m else None

        print(f"  HL: ter={ter} entry={entry} nav={nav}")
        return {"ter": ter, "entryCharge": entry, "exitCharge": "0.00%", "perf1y": perf, "srri": None, "nav": nav}
    except Exception as e:
        print(f"  HL error: {e}")
        return None

# ── Fidelity ──────────────────────────────────────────────────
def fetch_fidelity(isin):
    try:
        url = f"https://www.fidelity.co.uk/factsheet-data/factsheet/{isin}/key-statistics"
        r = SESSION.get(url, timeout=15, allow_redirects=True)
        text = r.text

        if r.status_code != 200:
            s = SESSION.get(f"https://www.fidelity.co.uk/search/?q={isin}", timeout=15)
            m = re.search(r'href="(/factsheet-data/factsheet/[^"]+/key-statistics)"', s.text)
            if not m:
                return None
            r = SESSION.get("https://www.fidelity.co.uk" + m.group(1), timeout=15)
            text = r.text

        ter_m = re.search(r'[Oo]ngoing charge \(%\)[^\d]+([\d.]+)', text)
        ter = to_pct(ter_m.group(1)) if ter_m else None
        entry_m = re.search(r'[Ff]und provider buy charge \(%\)[^\d]+([\d.]+)', text)
        entry = to_pct(entry_m.group(1)) if entry_m else "0.00%"

        return {"ter": ter, "entryCharge": entry, "exitCharge": "0.00%", "perf1y": None, "srri": None, "nav": None}
    except Exception as e:
        print(f"  Fidelity error: {e}")
        return None

# ── Interactive Investor ──────────────────────────────────────
def fetch_ii(isin):
    """ii URL pattern: ii.co.uk/funds/{slug}/{SEDOL} - use Playwright to find slug."""
    try:
        sedol = get_sedol(isin)
        print(f"  ii SEDOL: {sedol}")

        if PLAYWRIGHT_AVAILABLE:
            # Use Playwright to search ii
            search_url = f"https://www.ii.co.uk/funds?search={isin}"
            content, final_url = fetch_page_with_playwright(search_url)
            if content:
                # Find fund link with matching SEDOL
                m = re.search(r'href="(/funds/[a-z0-9\-]+/' + re.escape(sedol) + r')"', content, re.IGNORECASE)
                if m:
                    fund_url = "https://www.ii.co.uk" + m.group(1)
                    print(f"  ii fund URL: {fund_url}")
                    fund_content, _ = fetch_page_with_playwright(fund_url)
                    if fund_content:
                        ter = extract_ocf(fund_content)
                        return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

        # Requests fallback
        r = SESSION.get(f"https://www.ii.co.uk/funds?search={isin}", timeout=15)
        m = re.search(r'href="(/funds/[a-z0-9\-]+/' + re.escape(sedol) + r')"', r.text, re.IGNORECASE)
        if m:
            r2 = SESSION.get("https://www.ii.co.uk" + m.group(1), timeout=15)
            ter = extract_ocf(r2.text)
            return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

        return None
    except Exception as e:
        print(f"  ii error: {e}")
        return None

# ── Trustnet ──────────────────────────────────────────────────
def fetch_trustnet(isin):
    """Trustnet - use Playwright since their search requires JS."""
    try:
        if PLAYWRIGHT_AVAILABLE:
            url = f"https://www.trustnet.com/search/?query={isin}"
            content, final_url = fetch_page_with_playwright(url, wait_for='.fund-name')
            if content:
                print(f"  Trustnet URL: {final_url}")
                # Check if redirected to fund page
                if 'factsheets' in (final_url or ''):
                    ter = extract_ocf(content)
                    return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
                # Find fund link
                m = re.search(r'href="(/factsheets/[^"?#]+)"', content)
                if m:
                    fund_url = "https://www.trustnet.com" + m.group(1)
                    print(f"  Trustnet fund: {fund_url}")
                    fund_content, _ = fetch_page_with_playwright(fund_url)
                    if fund_content:
                        ter = extract_ocf(fund_content)
                        return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

        return None
    except Exception as e:
        print(f"  Trustnet error: {e}")
        return None

# ── Morningstar ───────────────────────────────────────────────
def fetch_morningstar(isin):
    """Morningstar - fix the list response parsing."""
    try:
        # Get fund ID
        search_url = f"https://www.morningstar.co.uk/uk/funds/SecuritySearchResults.aspx?type=ALL&search={isin}"
        r = SESSION.get(search_url, timeout=15)
        m = re.search(r'href="[^"]*snapshot\.aspx\?id=([^"&]+)"', r.text)
        if not m:
            return None

        fund_id = m.group(1)
        print(f"  Morningstar fund ID: {fund_id}")

        # Use their performance data API
        api_url = f"https://lt.morningstar.com/api/rest.svc/klr5zyak8x/security/screener?field=OngoingCharge&id={fund_id}&idtype=msid&languageId=en-GB&locale=en-GB&clientId=MDC&version=3.37.0&outputType=json"
        r2 = SESSION.get(api_url, timeout=15, headers={
            **HEADERS,
            "Accept": "application/json",
            "Referer": "https://www.morningstar.co.uk/",
            "X-Requested-With": "XMLHttpRequest"
        })
        print(f"  Morningstar API: {r2.status_code}")

        if r2.status_code == 200:
            try:
                data = r2.json()
                # Response is a list of fund objects
                items = data if isinstance(data, list) else [data]
                for item in items:
                    if isinstance(item, dict):
                        ocf = item.get('OngoingCharge') or item.get('ongoingCharge')
                        if ocf is not None:
                            try:
                                f = float(str(ocf))
                                if 0 < f < 10:
                                    ter = f"{f:.2f}%"
                                    print(f"  Morningstar OCF: {ter}")
                                    return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
                            except:
                                pass
            except Exception as je:
                print(f"  Morningstar JSON error: {je}")
                print(f"  Raw: {r2.text[:500]}")

        # Playwright fallback
        if PLAYWRIGHT_AVAILABLE:
            snap_url = f"https://www.morningstar.co.uk/uk/funds/snapshot/snapshot.aspx?id={fund_id}"
            content, _ = fetch_page_with_playwright(snap_url, wait_for='.sal-mip-overview')
            if content:
                ter = extract_ocf(content)
                if ter:
                    print(f"  Morningstar OCF from Playwright: {ter}")
                    return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

        return None
    except Exception as e:
        print(f"  Morningstar error: {e}")
        return None

# ── Routes ────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "service": "FundIntel Server v11",
        "playwright": PLAYWRIGHT_AVAILABLE
    })

@app.route('/fetch-platform-data', methods=['GET'])
def fetch_platform_data():
    isin = request.args.get('isin', '').strip().upper()
    if not isin or len(isin) < 10:
        return jsonify({"error": "Valid ISIN required"}), 400

    print(f"\n{'='*50}\nFetching: {isin}\n{'='*50}")
    results = {}

    for name, fetcher in [
        ("Hargreaves Lansdown", fetch_hl),
        ("Fidelity Personal Investing", fetch_fidelity),
        ("Interactive Investor", fetch_ii),
        ("Trustnet", fetch_trustnet),
        ("Morningstar", fetch_morningstar),
    ]:
        print(f"\n→ {name}...")
        try:
            data = fetcher(isin)
            norm = normalise(data)
            if norm:
                results[name] = norm
                print(f"  ✓ {norm}")
            else:
                print(f"  ✗ No data")
        except Exception as e:
            print(f"  ✗ Error: {e}")
        time.sleep(1)

    print(f"\nDone. {len(results)} platforms found.\n")
    return jsonify(results)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print("=" * 50)
    print(f"  FundIntel Backend Server v11")
    print(f"  Running on port {port}")
    print(f"  Playwright: {PLAYWRIGHT_AVAILABLE}")
    print("=" * 50)
    app.run(host='0.0.0.0', port=port, debug=False)
