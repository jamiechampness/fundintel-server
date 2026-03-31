#!/usr/bin/env python3
"""
FundIntel Backend Server v13
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

def google_find_url(query, site):
    """Use Google to find a fund page URL on a specific site."""
    try:
        search = f"https://www.google.com/search?q={requests.utils.quote(query)}+site:{site}"
        r = SESSION.get(search, timeout=10, headers={
            **HEADERS,
            "Accept": "text/html",
        })
        # Find URLs from the target site in Google results
        pattern = f'https?://(?:www\\.)?{re.escape(site)}[^"&\\s]+'
        urls = re.findall(pattern, r.text)
        # Filter out Google redirect URLs and return first real match
        for url in urls:
            if site in url and 'google' not in url:
                # Clean up any HTML entities
                url = url.replace('&amp;', '&').split('"')[0]
                return url
        return None
    except Exception as e:
        print(f"  Google search error: {e}")
        return None

# ── Hargreaves Lansdown ───────────────────────────────────────
def fetch_hl(isin):
    """
    HL: The search page shows sorted results. We need the specific fund page.
    Use Google to find the direct HL fund page URL for this ISIN.
    """
    try:
        sedol = get_sedol(isin)
        print(f"  HL SEDOL: {sedol}")

        # Use Google to find the direct HL fund page
        fund_url = google_find_url(f"{isin} fund charges", "hl.co.uk/funds/fund-discounts")
        
        if not fund_url:
            # Fallback: use the search page with Playwright
            search_url = f"https://www.hl.co.uk/funds/fund-discounts,-prices--and--factsheets/search-results?ISINsearch={isin}"
            html, final_url = pw_fetch(search_url, wait_ms=4000)
            if html:
                links = re.findall(r'href="(/funds/fund-discounts[^"]+/[a-z][a-z0-9\-]+)"', html, re.IGNORECASE)
                for link in links:
                    skip = ['/invest', '/key-features', '/charts', '/research', '/costs', '/fund-analysis']
                    if not any(x in link for x in skip) and link.count('/') >= 5:
                        fund_url = "https://www.hl.co.uk" + link
                        break

        if not fund_url:
            return None

        print(f"  HL fund URL: {fund_url}")
        html, _ = pw_fetch(fund_url, wait_ms=3000)
        if not html:
            r = SESSION.get(fund_url, timeout=15)
            html = r.text

        # Extract gross OCF - text between "OCF/TER" and "saving from HL"
        ter = None
        pos = html.find('OCF/TER')
        if pos > -1:
            # Find end before "saving" keyword
            saving_pos = html.find('saving', pos)
            net_pos = html.find('Net ongoing', pos)
            end = min(
                saving_pos if saving_pos > pos else pos + 300,
                net_pos if net_pos > pos else pos + 300
            )
            snippet = html[pos:end if end > pos else pos + 300]
            m = re.search(r'([\d]+\.[\d]{1,2})', snippet)
            if m:
                f = float(m.group(1))
                if 0.05 < f < 5.0:
                    ter = f"{f:.2f}%"

        entry_m = re.search(r'Net initial charge[^\d]*([\d.]+)%', html)
        entry = to_pct(entry_m.group(1)) if entry_m else None
        nav_m = re.search(r'Sell:([\d,]+\.?\d*p)', html)
        nav = nav_m.group(1) if nav_m else None
        perf_m = re.search(r'20/03/25 to 20/03/26[^0-9+\-]*([\+\-]?[\d.]+)%', html)
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
    """ii: Use Google to find the correct fund page URL."""
    try:
        sedol = get_sedol(isin)
        print(f"  ii SEDOL: {sedol}")

        # Use Google to find ii fund page
        fund_url = google_find_url(f"{isin} fund", f"ii.co.uk/funds")
        
        if not fund_url:
            # Try direct SEDOL URL pattern
            fund_url = f"https://www.ii.co.uk/funds/{sedol}"

        print(f"  ii fund URL: {fund_url}")
        
        if PLAYWRIGHT_AVAILABLE:
            html, final_url = pw_fetch(fund_url, wait_ms=4000)
            if html and 'funds' in (final_url or ''):
                ter = extract_ocf(html)
                print(f"  ii ter: {ter}")
                return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

        r = SESSION.get(fund_url, timeout=15, allow_redirects=True)
        if r.status_code == 200:
            ter = extract_ocf(r.text)
            return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

        return None
    except Exception as e:
        print(f"  ii error: {e}")
        return None

# ── Trustnet ──────────────────────────────────────────────────
def fetch_trustnet(isin):
    """Trustnet: Use Google to find fund page."""
    try:
        fund_url = google_find_url(f"{isin} fund factsheet", "trustnet.com/factsheets")
        
        if fund_url:
            print(f"  Trustnet fund URL: {fund_url}")
            if PLAYWRIGHT_AVAILABLE:
                html, _ = pw_fetch(fund_url, wait_ms=4000)
            else:
                r = SESSION.get(fund_url, timeout=15)
                html = r.text
            if html:
                ter = extract_ocf(html)
                return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

        return None
    except Exception as e:
        print(f"  Trustnet error: {e}")
        return None

# ── Morningstar ───────────────────────────────────────────────
def fetch_morningstar(isin):
    """Morningstar: Use Playwright to render the full snapshot page."""
    try:
        search_url = f"https://www.morningstar.co.uk/uk/funds/SecuritySearchResults.aspx?type=ALL&search={isin}"
        r = SESSION.get(search_url, timeout=15)
        m = re.search(r'href="[^"]*snapshot\.aspx\?id=([^"&]+)"', r.text)
        if not m:
            return None

        fund_id = m.group(1)
        print(f"  Morningstar fund ID: {fund_id}")

        if PLAYWRIGHT_AVAILABLE:
            snap_url = f"https://www.morningstar.co.uk/uk/funds/snapshot/snapshot.aspx?id={fund_id}"
            html, _ = pw_fetch(snap_url, wait_ms=6000)
            if html:
                # Try JSON data in page
                json_m = re.search(r'"OngoingCharge"\s*[=:]\s*["\']?([0-9.]+)["\']?', html)
                if json_m:
                    ter = to_pct(json_m.group(1))
                    print(f"  Morningstar OCF from JSON: {ter}")
                    return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
                ter = extract_ocf(html)
                if ter:
                    print(f"  Morningstar OCF from HTML: {ter}")
                    return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

        return None
    except Exception as e:
        print(f"  Morningstar error: {e}")
        return None

# ── Routes ────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "FundIntel Server v13", "playwright": PLAYWRIGHT_AVAILABLE})

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
    print(f"  FundIntel Backend Server v13")
    print(f"  Running on port {port}")
    print(f"  Playwright: {PLAYWRIGHT_AVAILABLE}")
    print("=" * 50)
    app.run(host='0.0.0.0', port=port, debug=False)
