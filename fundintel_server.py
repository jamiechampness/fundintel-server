# FundIntel Backend Server v10
#!/usr/bin/env python3
"""
FundIntel Backend Server v10.1
Run with: python3 fundintel_server.py
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
import re
import time
import json
import os
import subprocess

# Install Playwright browsers on first run if needed
try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

app = Flask(__name__)
CORS(app)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

def get_sedol(isin):
    """Extract SEDOL from ISIN. GB ISINs: GB + 00 + SEDOL(7) + check(1)"""
    if isin.startswith('GB') and len(isin) == 12:
        return isin[4:11]  # e.g. GB00B0XWNG99 -> B0XWNG9
    return isin[2:9]  # fallback for other ISINs

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

# ── Hargreaves Lansdown ───────────────────────────────────────
def fetch_hl(isin):
    """
    HL: The ISIN search page redirects to the fund page in a browser but
    not server-side. Use the known URL pattern instead:
    hl.co.uk/funds/fund-discounts.../search-results/a/{fund-slug}
    We derive the slug from the fund name extracted from search results.
    """
    try:
        sedol = get_sedol(isin)
        print(f"  HL SEDOL: {sedol}")

        # HL search returns fund data in HTML table even on search results page
        url = f"https://www.hl.co.uk/funds/fund-discounts,-prices--and--factsheets/search-results?ISINsearch={isin}"
        r = SESSION.get(url, timeout=15, allow_redirects=True)
        print(f"  HL URL: {r.url} [{r.status_code}]")
        text = r.text

        # Extract fund page link from search results
        # HL search results have links like /funds/fund-discounts.../search-results/a/fund-name
        links = re.findall(
            r'href="(/funds/fund-discounts[^"]+/[a-z][a-z0-9\-]+)"',
            text, re.IGNORECASE
        )
        fund_link = None
        for link in links:
            skip = ['/invest', '/key-features', '/charts', '/research', '/costs',
                    '/fund-analysis', 'ISINsearch', '?', '#']
            if not any(x in link for x in skip) and link.count('/') >= 5:
                fund_link = link
                break

        if fund_link:
            r2 = SESSION.get("https://www.hl.co.uk" + fund_link, timeout=15)
            print(f"  HL fund page: {r2.url}")
            text = r2.text
        else:
            print(f"  HL: no fund link found, trying SEDOL URL")
            # Try HL's SEDOL-based factsheet PDF URL to find the fund name
            r3 = SESSION.get(
                f"https://www.hl.co.uk/funds/fund-discounts,-prices--and--factsheets/search-results?tab=prices&ISINsearch={isin}",
                timeout=15, allow_redirects=True
            )
            text = r3.text

        ter = extract_ocf(text)

        # HL specific: also try the markdown table format
        if not ter:
            m = re.search(r'Ongoing charge \(OCF/TER\)[^\d]+([\d.]+)%', text)
            if m:
                ter = to_pct(m.group(1))

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
    """
    ii URL: ii.co.uk/funds/{slug}/{SEDOL}
    SEDOL for GB ISINs is at positions 4-11.
    """
    try:
        sedol = get_sedol(isin)
        print(f"  ii SEDOL: {sedol}")

        # Try the known URL pattern directly
        # ii search: ii.co.uk/funds?search=ISIN returns JSON or HTML with links
        search_url = f"https://www.ii.co.uk/funds?search={isin}"
        r = SESSION.get(search_url, timeout=15, allow_redirects=True)
        print(f"  ii search: {r.url} [{r.status_code}]")

        # Find link containing our SEDOL
        m = re.search(
            r'href="(/funds/[a-z0-9\-]+/' + re.escape(sedol) + r')"',
            r.text, re.IGNORECASE
        )

        if not m:
            # Try their API search
            api_url = f"https://www.ii.co.uk/api/search?q={isin}&type=fund"
            r2 = SESSION.get(api_url, timeout=15,
                headers={**HEADERS, "Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
            print(f"  ii API: {r2.status_code}")
            if r2.status_code == 200:
                try:
                    data = r2.json()
                    # Look for fund URL in response
                    text_data = json.dumps(data)
                    m2 = re.search(r'/funds/([a-z0-9\-]+)/' + re.escape(sedol), text_data, re.IGNORECASE)
                    if m2:
                        fund_url = f"https://www.ii.co.uk/funds/{m2.group(1)}/{sedol}"
                        r3 = SESSION.get(fund_url, timeout=15)
                        text = r3.text
                        ter = extract_ocf(text)
                        return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
                except:
                    pass
            return None

        fund_url = "https://www.ii.co.uk" + m.group(1)
        print(f"  ii fund URL: {fund_url}")
        r2 = SESSION.get(fund_url, timeout=15)
        ter = extract_ocf(r2.text)

        return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
    except Exception as e:
        print(f"  ii error: {e}")
        return None

# ── Trustnet ──────────────────────────────────────────────────
def fetch_trustnet(isin):
    """
    Trustnet: use their main site search.
    """
    try:
        # Try Trustnet's fund finder with ISIN
        urls_to_try = [
            f"https://www.trustnet.com/factsheets/f/search?search={isin}",
            f"https://www.trustnet.com/factsheets/f/search?isin={isin}",
            f"https://www.trustnet.com/search/?query={isin}",
        ]

        for url in urls_to_try:
            r = SESSION.get(url, timeout=15, allow_redirects=True)
            print(f"  Trustnet: {r.url} [{r.status_code}]")

            if r.status_code == 200:
                # If redirected to fund page
                if '/factsheets/' in r.url and 'search' not in r.url:
                    ter = extract_ocf(r.text)
                    if ter:
                        return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

                # Find fund link in results
                m = re.search(r'href="(/factsheets/[^"?#\s]{5,})"', r.text)
                if m:
                    fund_url = "https://www.trustnet.com" + m.group(1)
                    print(f"  Trustnet fund: {fund_url}")
                    r2 = SESSION.get(fund_url, timeout=15)
                    ter = extract_ocf(r2.text)
                    return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

        return None
    except Exception as e:
        print(f"  Trustnet error: {e}")
        return None

# ── Morningstar ───────────────────────────────────────────────
def fetch_morningstar(isin):
    """
    Morningstar: use their screener API with correct request format.
    The API returns a list of objects with fund data.
    """
    try:
        # Get fund ID from search
        search_url = f"https://www.morningstar.co.uk/uk/funds/SecuritySearchResults.aspx?type=ALL&search={isin}"
        r = SESSION.get(search_url, timeout=15)
        m = re.search(r'href="[^"]*snapshot\.aspx\?id=([^"&]+)"', r.text)
        if not m:
            return None

        fund_id = m.group(1)
        print(f"  Morningstar fund ID: {fund_id}")

        # Use Morningstar's performance API - returns actual fund data
        perf_url = f"https://lt.morningstar.com/api/rest.svc/klr5zyak8x/security/screener?field=OngoingCharge&id={fund_id}&idtype=msid&languageId=en-GB&locale=en-GB&clientId=MDC&version=3.37.0&outputType=json"
        r2 = SESSION.get(perf_url, timeout=15,
            headers={**HEADERS,
                "Accept": "application/json, text/javascript, */*",
                "Referer": "https://www.morningstar.co.uk/",
                "X-Requested-With": "XMLHttpRequest"
            })
        print(f"  Morningstar screener API: {r2.status_code}")

        if r2.status_code == 200:
            try:
                data = r2.json()
                print(f"  Morningstar data type: {type(data)}")
                # The response is a list of fund objects
                if isinstance(data, list) and len(data) > 0:
                    fund = data[0]
                    ocf = fund.get('OngoingCharge') or fund.get('ongoingCharge')
                    if ocf is not None:
                        f = float(str(ocf))
                        if 0 < f < 10:
                            ter = f"{f:.2f}%"
                            print(f"  Morningstar OCF: {ter}")
                            return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
                elif isinstance(data, dict):
                    ocf = data.get('OngoingCharge') or data.get('ongoingCharge')
                    if ocf is not None:
                        ter = to_pct(ocf)
                        if ter:
                            return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
            except Exception as je:
                print(f"  Morningstar parse: {je} | raw: {r2.text[:300]}")

        # Fallback: HTML snapshot
        snap_url = f"https://www.morningstar.co.uk/uk/funds/snapshot/snapshot.aspx?id={fund_id}"
        r3 = SESSION.get(snap_url, timeout=15)
        ter = extract_ocf(r3.text)
        if ter:
            print(f"  Morningstar OCF from HTML: {ter}")
        return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

    except Exception as e:
        print(f"  Morningstar error: {e}")
        return None

# ── Routes ────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "FundIntel Server v10"})

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
    import os
    port = int(os.environ.get('PORT', 8080))
    print("=" * 50)
    print(f"  FundIntel Backend Server v10")
    print(f"  Running on port {port}")
    print("=" * 50)
    app.run(host='0.0.0.0', port=port, debug=False)
