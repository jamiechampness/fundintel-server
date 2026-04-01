#!/usr/bin/env python3
"""FundIntel Backend Server v14"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests, re, time, os

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
    return isin[4:11] if (isin.startswith('GB') and len(isin) == 12) else isin[2:9]

def to_pct(val):
    if val is None: return None
    try:
        f = float(str(val).strip().rstrip('%').replace(',',''))
        return f"{f:.2f}%"
    except: return None

def normalise(data):
    if not data: return None
    out = {k: data.get(k) for k in ("ter","entryCharge","exitCharge","perf1y","srri","nav")}
    return out if any(v is not None for v in out.values()) else None

def extract_ocf(text):
    """Find first realistic OCF % near relevant keywords."""
    for kw in ['OCF/TER','Ongoing charge','Ongoing Charge','ongoing charge','OCF','TER']:
        pos = text.find(kw)
        if pos > -1:
            for m in re.finditer(r'(\d+\.\d{1,3})', text[pos:pos+400]):
                try:
                    f = float(m.group(1))
                    if 0.05 < f < 5.0:
                        return f"{f:.2f}%"
                except: pass
    return None

def pw_get(url, wait_for_text=None, wait_ms=5000):
    """Fetch URL with Playwright. Optionally wait for specific text to appear."""
    if not PLAYWRIGHT_AVAILABLE:
        return None, None
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(headless=True)
            ctx = br.new_context(user_agent=HEADERS["User-Agent"])
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=30000)
            if wait_for_text:
                # Poll for the text to appear (JS rendering)
                for _ in range(20):
                    if wait_for_text in pg.content():
                        break
                    pg.wait_for_timeout(500)
            else:
                pg.wait_for_timeout(wait_ms)
            html = pg.content()
            final_url = pg.url
            br.close()
            return html, final_url
    except Exception as e:
        print(f"  PW error: {e}")
        return None, None

# ── Hargreaves Lansdown ────────────────────────────────────────
def fetch_hl(isin):
    """
    Direct URL: hl.co.uk/funds/fund-discounts.../search-results/a/{fund-slug}
    The slug is derived from the fund name. We use HL's search API to get it.
    HL has a JSON search endpoint we can use to find the slug.
    """
    try:
        sedol = get_sedol(isin)
        print(f"  HL SEDOL: {sedol}")

        # HL has an internal search API that returns JSON
        api_url = f"https://www.hl.co.uk/funds/fund-discounts,-prices--and--factsheets/search-results?ISINsearch={isin}&output=json"
        r = SESSION.get(api_url, timeout=15)
        
        # Also try their autocomplete endpoint
        ac_url = f"https://www.hl.co.uk/ajax/autocomplete?search={isin}&type=fund"
        r2 = SESSION.get(ac_url, timeout=10, headers={**HEADERS, "X-Requested-With": "XMLHttpRequest"})
        print(f"  HL autocomplete: {r2.status_code} | {r2.text[:200]}")

        # Known working URL pattern for this fund
        # Use Playwright to load it and wait for OCF text to appear
        fund_url = f"https://www.hl.co.uk/funds/fund-discounts,-prices--and--factsheets/search-results?ISINsearch={isin}"
        html, final_url = pw_get(fund_url, wait_for_text="Ongoing charge", wait_ms=8000)
        
        if html:
            print(f"  HL URL: {final_url}")
            print(f"  HL OCF/TER in page: {'OCF/TER' in html}")
            print(f"  HL Ongoing in page: {'Ongoing charge' in html}")
            
            # If still on search results page, follow the fund link
            if 'ISINsearch' in final_url or 'start=0' in final_url:
                links = re.findall(r'href="(/funds/fund-discounts[^"]+/[a-z][a-z0-9\-]+)"', html, re.IGNORECASE)
                for link in links:
                    if not any(x in link for x in ['/invest','/key-features','/charts','/research','/costs','/fund-analysis','?']):
                        if link.count('/') >= 5:
                            fund_page = "https://www.hl.co.uk" + link
                            print(f"  HL following: {fund_page}")
                            html, final_url = pw_get(fund_page, wait_for_text="Ongoing charge", wait_ms=8000)
                            break

            if html:
                # Extract gross OCF - find between OCF/TER and saving
                ter = None
                pos = html.find('OCF/TER')
                if pos == -1:
                    pos = html.find('Ongoing charge')
                if pos > -1:
                    saving_pos = html.find('saving', pos)
                    end = saving_pos if (saving_pos > pos and saving_pos < pos + 500) else pos + 300
                    snippet = html[pos:end]
                    for m in re.finditer(r'(\d+\.\d{1,2})', snippet):
                        f = float(m.group(1))
                        if 0.05 < f < 5.0:
                            ter = f"{f:.2f}%"
                            break
                
                entry_m = re.search(r'Net initial charge[^\d]*([\d.]+)%', html)
                entry = to_pct(entry_m.group(1)) if entry_m else None
                nav_m = re.search(r'Sell:([\d,]+\.?\d*p)', html)
                nav = nav_m.group(1) if nav_m else None

                print(f"  HL: ter={ter} entry={entry}")
                return {"ter": ter, "entryCharge": entry, "exitCharge": "0.00%", "perf1y": None, "srri": None, "nav": nav}

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
            if not m: return None
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

# ── Interactive Investor ───────────────────────────────────────
def fetch_ii(isin):
    """
    Known URL: ii.co.uk/funds/abrdn-asia-pacific-equity-i-acc/B0XWNG9
    Use Playwright and wait for OCF text.
    """
    try:
        sedol = get_sedol(isin)
        print(f"  ii SEDOL: {sedol}")

        # Try direct SEDOL URL - ii sometimes accepts this
        for url in [
            f"https://www.ii.co.uk/funds/{sedol}",
            f"https://www.ii.co.uk/investments/funds/{isin}",
        ]:
            html, final_url = pw_get(url, wait_for_text="Ongoing", wait_ms=6000)
            if html and "Ongoing" in (html or ""):
                print(f"  ii URL: {final_url}")
                ter = extract_ocf(html)
                print(f"  ii ter: {ter}")
                if ter:
                    return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

        # Try ii search with Playwright  
        search_url = f"https://www.ii.co.uk/funds?search={sedol}"
        html, final_url = pw_get(search_url, wait_ms=6000)
        if html:
            print(f"  ii search URL: {final_url}")
            # Find fund link
            m = re.search(r'href="(/funds/[a-z0-9\-]+/' + re.escape(sedol) + r')"', html, re.IGNORECASE)
            if m:
                fund_url = "https://www.ii.co.uk" + m.group(1)
                print(f"  ii fund: {fund_url}")
                fund_html, _ = pw_get(fund_url, wait_for_text="Ongoing", wait_ms=6000)
                if fund_html:
                    ter = extract_ocf(fund_html)
                    return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

        return None
    except Exception as e:
        print(f"  ii error: {e}")
        return None

# ── Trustnet ──────────────────────────────────────────────────
def fetch_trustnet(isin):
    """Use Playwright with Trustnet's ISIN search."""
    try:
        # Trustnet search by ISIN - use Playwright
        url = f"https://www.trustnet.com/factsheets/f/search?search={isin}"
        html, final_url = pw_get(url, wait_ms=6000)
        print(f"  Trustnet URL: {final_url}")
        
        if html:
            # If redirected to fund page
            if final_url and 'search' not in final_url and 'factsheets' in final_url:
                ter = extract_ocf(html)
                return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
            
            # Find fund link in results
            m = re.search(r'href="(/factsheets/[^"?#]{5,})"', html)
            if m:
                fund_url = "https://www.trustnet.com" + m.group(1)
                print(f"  Trustnet fund: {fund_url}")
                fund_html, _ = pw_get(fund_url, wait_for_text="Ongoing", wait_ms=6000)
                if fund_html:
                    ter = extract_ocf(fund_html)
                    return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
            else:
                print(f"  Trustnet: no fund link in results | page length: {len(html)}")

        return None
    except Exception as e:
        print(f"  Trustnet error: {e}")
        return None

# ── Morningstar ───────────────────────────────────────────────
def fetch_morningstar(isin):
    """Use Playwright to render Morningstar fund page and extract OCF."""
    try:
        # Get fund ID from search
        r = SESSION.get(
            f"https://www.morningstar.co.uk/uk/funds/SecuritySearchResults.aspx?type=ALL&search={isin}",
            timeout=15
        )
        m = re.search(r'href="[^"]*snapshot\.aspx\?id=([^"&]+)"', r.text)
        if not m:
            return None

        fund_id = m.group(1)
        print(f"  Morningstar ID: {fund_id}")

        # Use Playwright to render the snapshot page
        snap_url = f"https://www.morningstar.co.uk/uk/funds/snapshot/snapshot.aspx?id={fund_id}"
        html, _ = pw_get(snap_url, wait_for_text="Ongoing", wait_ms=8000)
        
        if html:
            print(f"  Morningstar 'Ongoing' in page: {'Ongoing' in html}")
            print(f"  Morningstar page length: {len(html)}")
            
            # Try to find OCF in rendered page
            ter = extract_ocf(html)
            
            # Also try JSON patterns
            if not ter:
                for pat in [r'"OngoingCharge"\s*[=:]\s*["\']?(\d+\.\d+)', r'ongoingCharge["\s:]+(\d+\.\d+)']:
                    jm = re.search(pat, html)
                    if jm:
                        ter = to_pct(jm.group(1))
                        break

            print(f"  Morningstar ter: {ter}")
            return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

        return None
    except Exception as e:
        print(f"  Morningstar error: {e}")
        return None

# ── Routes ────────────────────────────────────────────────────
@app.route('/health')
def health():
    return {"status": "ok", "service": "FundIntel Server v14", "playwright": PLAYWRIGHT_AVAILABLE}

@app.route('/fetch-platform-data')
def fetch_platform_data():
    isin = request.args.get('isin', '').strip().upper()
    if not isin or len(isin) < 10:
        return {"error": "Valid ISIN required"}, 400

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

    print(f"\nDone. {len(results)} platforms.\n")
    return results

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f"{'='*50}\n  FundIntel v14 | port {port} | playwright={PLAYWRIGHT_AVAILABLE}\n{'='*50}")
    app.run(host='0.0.0.0', port=port, debug=False)
