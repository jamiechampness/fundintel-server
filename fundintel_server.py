#!/usr/bin/env python3
"""
FundIntel Backend Server v12
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
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
    print("✓ Playwright available")
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("✗ Playwright not available")

def get_sedol(isin):
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

def pw_fetch(url, wait_ms=3000):
    """Fetch URL with Playwright, return (html, final_url)."""
    if not PLAYWRIGHT_AVAILABLE:
        return None, None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(wait_ms)
            html = page.content()
            final = page.url
            browser.close()
            return html, final
    except Exception as e:
        print(f"  Playwright fetch error: {e}")
        return None, None

# ── Hargreaves Lansdown ───────────────────────────────────────
def fetch_hl(isin):
    """
    HL: Search lands on sorted results page, not fund page.
    Need to find and click the fund link from search results.
    We know the fund page URL pattern from earlier manual research.
    Use Playwright to extract the first fund link from search results.
    """
    try:
        sedol = get_sedol(isin)
        print(f"  HL SEDOL: {sedol}")

        search_url = f"https://www.hl.co.uk/funds/fund-discounts,-prices--and--factsheets/search-results?ISINsearch={isin}"

        if PLAYWRIGHT_AVAILABLE:
            html, final_url = pw_fetch(search_url, wait_ms=4000)
            if html:
                print(f"  HL search URL: {final_url}")
                # Find the first fund link in the search results table
                # HL renders results as links like /funds/fund-discounts.../search-results/a/fund-name
                links = re.findall(
                    r'href="(/funds/fund-discounts[^"]+/[a-z][a-z0-9\-]+)"',
                    html, re.IGNORECASE
                )
                fund_link = None
                for link in links:
                    skip = ['/invest', '/key-features', '/charts', '/research',
                            '/costs', '/fund-analysis', 'ISINsearch', '?tab']
                    if not any(x in link for x in skip) and link.count('/') >= 5:
                        fund_link = link
                        break

                if fund_link:
                    fund_url = "https://www.hl.co.uk" + fund_link
                    print(f"  HL fund page: {fund_url}")
                    fund_html, _ = pw_fetch(fund_url, wait_ms=3000)
                    if fund_html:
                        html = fund_html

                # Extract OCF - specifically the GROSS charge (not net after HL discount)
                # In HL's HTML: "Ongoing charge (OCF/TER):" then the gross figure
                # Then "Ongoing saving from HL:" then "Net ongoing charge:"
                # We want the FIRST percentage after "OCF/TER"
                ter = None
                pos = html.find('OCF/TER')
                if pos > -1:
                    # Get text between OCF/TER and "saving" or "Net"
                    end = min(
                        html.find('saving', pos) if html.find('saving', pos) > -1 else len(html),
                        html.find('Net ongoing', pos) if html.find('Net ongoing', pos) > -1 else len(html)
                    )
                    snippet = html[pos:pos+300] if end == len(html) else html[pos:end]
                    m = re.search(r'([\d]+\.[\d]{1,2})%', snippet)
                    if m:
                        ter = to_pct(m.group(1))

                if not ter:
                    ter = extract_ocf(html)

                entry_m = re.search(r'Net initial charge[^\d]*([\d.]+)%', html)
                entry = to_pct(entry_m.group(1)) if entry_m else None
                nav_m = re.search(r'Sell:([\d,]+\.?\d*p)', html)
                nav = nav_m.group(1) if nav_m else None
                perf_m = re.search(r'20/03/25 to 20/03/26[^0-9+\-]*([\+\-]?[\d.]+)%', html)
                perf = f"{perf_m.group(1)}%" if perf_m else None

                print(f"  HL: ter={ter} entry={entry} nav={nav}")
                return {"ter": ter, "entryCharge": entry, "exitCharge": "0.00%", "perf1y": perf, "srri": None, "nav": nav}

        return None
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
    """ii: Use Playwright to search and find the fund page."""
    try:
        sedol = get_sedol(isin)
        print(f"  ii SEDOL: {sedol}")

        if PLAYWRIGHT_AVAILABLE:
            # ii search page
            search_url = f"https://www.ii.co.uk/funds?search={isin}"
            html, final_url = pw_fetch(search_url, wait_ms=4000)
            if html:
                print(f"  ii search URL: {final_url}")
                # Look for fund link with SEDOL
                m = re.search(
                    r'href="(/funds/[a-z0-9\-]+/' + re.escape(sedol) + r')"',
                    html, re.IGNORECASE
                )
                if not m:
                    # Try broader search - any fund link
                    links = re.findall(r'href="(/funds/[a-z0-9\-]+/[A-Z0-9]{7})"', html, re.IGNORECASE)
                    print(f"  ii links found: {links[:3]}")
                    # Find one matching our SEDOL
                    for link in links:
                        if sedol.upper() in link.upper():
                            m = type('M', (), {'group': lambda self, n, l=link: l})()
                            break

                if m:
                    fund_url = "https://www.ii.co.uk" + m.group(1)
                    print(f"  ii fund URL: {fund_url}")
                    fund_html, _ = pw_fetch(fund_url, wait_ms=3000)
                    if fund_html:
                        ter = extract_ocf(fund_html)
                        print(f"  ii ter: {ter}")
                        return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
                else:
                    print(f"  ii: no fund link found for SEDOL {sedol}")

        return None
    except Exception as e:
        print(f"  ii error: {e}")
        return None

# ── Trustnet ──────────────────────────────────────────────────
def fetch_trustnet(isin):
    """Trustnet: Use Playwright to search and navigate to fund page."""
    try:
        if PLAYWRIGHT_AVAILABLE:
            search_url = f"https://www.trustnet.com/search/?query={isin}"
            html, final_url = pw_fetch(search_url, wait_ms=5000)
            if html:
                print(f"  Trustnet search URL: {final_url}")

                # Check if redirected to fund page directly
                if '/factsheets/' in (final_url or '') and 'search' not in (final_url or ''):
                    ter = extract_ocf(html)
                    return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

                # Find factsheet link in results
                m = re.search(r'href="(https?://www\.trustnet\.com/factsheets/[^"?#]+)"', html)
                if not m:
                    m = re.search(r'href="(/factsheets/[^"?#]+)"', html)

                if m:
                    fund_url = m.group(1)
                    if not fund_url.startswith('http'):
                        fund_url = "https://www.trustnet.com" + fund_url
                    print(f"  Trustnet fund URL: {fund_url}")
                    fund_html, _ = pw_fetch(fund_url, wait_ms=3000)
                    if fund_html:
                        ter = extract_ocf(fund_html)
                        return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
                else:
                    print(f"  Trustnet: no fund link found in search results")

        return None
    except Exception as e:
        print(f"  Trustnet error: {e}")
        return None

# ── Morningstar ───────────────────────────────────────────────
def fetch_morningstar(isin):
    """Morningstar: Fix list parsing from screener API."""
    try:
        search_url = f"https://www.morningstar.co.uk/uk/funds/SecuritySearchResults.aspx?type=ALL&search={isin}"
        r = SESSION.get(search_url, timeout=15)
        m = re.search(r'href="[^"]*snapshot\.aspx\?id=([^"&]+)"', r.text)
        if not m:
            return None

        fund_id = m.group(1)
        print(f"  Morningstar fund ID: {fund_id}")

        # Try their fund data API - returns list of field schemas, not data
        # Use the correct data endpoint instead
        data_url = f"https://lt.morningstar.com/api/rest.svc/klr5zyak8x/security/screener?field=OngoingCharge&id={fund_id}&idtype=msid&languageId=en-GB&locale=en-GB&clientId=MDC&version=3.37.0"
        r2 = SESSION.get(data_url, timeout=15, headers={
            **HEADERS,
            "Accept": "application/json",
            "Referer": "https://www.morningstar.co.uk/",
            "X-Requested-With": "XMLHttpRequest"
        })
        print(f"  Morningstar API: {r2.status_code} | {r2.text[:200]}")

        # The API returns schema, not data - use Playwright instead
        if PLAYWRIGHT_AVAILABLE:
            snap_url = f"https://www.morningstar.co.uk/uk/funds/snapshot/snapshot.aspx?id={fund_id}"
            html, _ = pw_fetch(snap_url, wait_ms=5000)
            if html:
                # Morningstar renders OCF in a specific element
                ter = None
                # Try JSON data embedded in page
                json_m = re.search(r'"OngoingCharge"\s*:\s*([0-9.]+)', html)
                if json_m:
                    ter = to_pct(json_m.group(1))
                if not ter:
                    ter = extract_ocf(html)
                if ter:
                    print(f"  Morningstar OCF: {ter}")
                    return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

        return None
    except Exception as e:
        print(f"  Morningstar error: {e}")
        return None

# ── Routes ────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "FundIntel Server v12", "playwright": PLAYWRIGHT_AVAILABLE})

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
    print(f"  FundIntel Backend Server v12")
    print(f"  Running on port {port}")
    print(f"  Playwright: {PLAYWRIGHT_AVAILABLE}")
    print("=" * 50)
    app.run(host='0.0.0.0', port=port, debug=False)
