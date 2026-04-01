#!/usr/bin/env python3
"""FundIntel Backend Server v15"""

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

def pw_get(url, wait_ms=6000):
    """Fetch with Playwright, wait fixed time for JS to render."""
    if not PLAYWRIGHT_AVAILABLE:
        return None, None
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(headless=True)
            ctx = br.new_context(user_agent=HEADERS["User-Agent"])
            pg = ctx.new_page()
            pg.goto(url, wait_until="networkidle", timeout=30000)
            pg.wait_for_timeout(wait_ms)
            try:
                html = pg.content()
            except:
                pg.wait_for_timeout(3000)
                html = pg.content()
            final_url = pg.url
            br.close()
            return html, final_url
    except Exception as e:
        print(f"  PW error: {e}")
        return None, None

# ── Hargreaves Lansdown ────────────────────────────────────────
def fetch_hl(isin):
    try:
        sedol = get_sedol(isin)
        print(f"  HL SEDOL: {sedol}")

        # HL search page - use networkidle to ensure full load
        search_url = f"https://www.hl.co.uk/funds/fund-discounts,-prices--and--factsheets/search-results?ISINsearch={isin}"
        html, final_url = pw_get(search_url, wait_ms=5000)
        
        if not html:
            return None

        print(f"  HL URL: {final_url}")

        # If on search results page, find and follow the fund link
        if 'ISINsearch' in final_url or 'start=0' in final_url:
            links = re.findall(r'href="(/funds/fund-discounts[^"]+/[a-z][a-z0-9\-]+)"', html, re.IGNORECASE)
            for link in links:
                skip = ['/invest','/key-features','/charts','/research','/costs','/fund-analysis','?','#']
                if not any(x in link for x in skip) and link.count('/') >= 5:
                    fund_url = "https://www.hl.co.uk" + link
                    print(f"  HL fund page: {fund_url}")
                    html, final_url = pw_get(fund_url, wait_ms=5000)
                    break

        if not html:
            return None

        # HL renders OCF in a markdown-style table:
        # | Ongoing charge (OCF/TER): | **0.86%** |
        # | Ongoing saving from HL: | **0.30%** |  
        # | Net ongoing charge: | **0.56%** |
        # We need the FIRST percentage after OCF/TER, before "saving"
        
        ter = None
        
        # Method 1: Find OCF/TER row specifically
        ocf_pos = html.find('OCF/TER')
        saving_pos = html.find('saving', ocf_pos) if ocf_pos > -1 else -1
        
        if ocf_pos > -1:
            # Get text from OCF/TER to just before "saving"
            end = saving_pos if (saving_pos > ocf_pos and saving_pos < ocf_pos + 500) else ocf_pos + 200
            snippet = html[ocf_pos:end]
            print(f"  HL snippet: {snippet[:100]}")
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
    try:
        sedol = get_sedol(isin)
        print(f"  ii SEDOL: {sedol}")

        # Search by SEDOL - more reliable than ISIN on ii
        search_url = f"https://www.ii.co.uk/funds?search={sedol}"
        html, final_url = pw_get(search_url, wait_ms=6000)
        
        if not html:
            return None
            
        print(f"  ii search URL: {final_url}")

        # Find fund link matching our SEDOL
        m = re.search(r'href="(/funds/[a-z0-9\-]+/' + re.escape(sedol) + r')"', html, re.IGNORECASE)
        if not m:
            # Log what links we did find
            all_links = re.findall(r'href="(/funds/[^"]{5,})"', html)[:5]
            print(f"  ii links found: {all_links}")
            return None

        fund_url = "https://www.ii.co.uk" + m.group(1)
        print(f"  ii fund URL: {fund_url}")
        
        fund_html, _ = pw_get(fund_url, wait_ms=6000)
        if not fund_html:
            return None

        ter = extract_ocf(fund_html)
        print(f"  ii ter: {ter}")
        return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
    except Exception as e:
        print(f"  ii error: {e}")
        return None

# ── Trustnet ──────────────────────────────────────────────────
def fetch_trustnet(isin):
    try:
        # Trustnet ISIN search - 576KB page loads so JS is rendering
        url = f"https://www.trustnet.com/factsheets/f/search?search={isin}"
        html, final_url = pw_get(url, wait_ms=6000)
        
        if not html:
            return None
            
        print(f"  Trustnet URL: {final_url} | len: {len(html)}")

        # If redirected to fund page
        if final_url and 'search' not in final_url and '/factsheets/' in final_url:
            ter = extract_ocf(html)
            return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

        # Find fund link - log first few to debug
        all_links = re.findall(r'href="(/factsheets/[^"?#]{5,})"', html)
        print(f"  Trustnet links: {all_links[:5]}")
        
        if all_links:
            fund_url = "https://www.trustnet.com" + all_links[0]
            print(f"  Trustnet fund: {fund_url}")
            fund_html, _ = pw_get(fund_url, wait_ms=5000)
            if fund_html:
                ter = extract_ocf(fund_html)
                return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

        return None
    except Exception as e:
        print(f"  Trustnet error: {e}")
        return None

# ── Morningstar ───────────────────────────────────────────────
def fetch_morningstar(isin):
    try:
        r = SESSION.get(
            f"https://www.morningstar.co.uk/uk/funds/SecuritySearchResults.aspx?type=ALL&search={isin}",
            timeout=15
        )
        m = re.search(r'href="[^"]*snapshot\.aspx\?id=([^"&]+)"', r.text)
        if not m:
            return None

        fund_id = m.group(1)
        print(f"  Morningstar ID: {fund_id}")

        snap_url = f"https://www.morningstar.co.uk/uk/funds/snapshot/snapshot.aspx?id={fund_id}"
        html, _ = pw_get(snap_url, wait_ms=8000)
        
        if not html:
            return None

        print(f"  Morningstar page len: {len(html)} | Ongoing: {'Ongoing' in html}")

        ter = None
        for pat in [
            r'"OngoingCharge"\s*[=:]\s*["\']?(\d+\.\d+)',
            r'ongoingCharge["\s:]+(\d+\.\d+)',
        ]:
            jm = re.search(pat, html)
            if jm:
                ter = to_pct(jm.group(1))
                break
        
        if not ter:
            ter = extract_ocf(html)

        print(f"  Morningstar ter: {ter}")
        return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
    except Exception as e:
        print(f"  Morningstar error: {e}")
        return None

# ── Routes ────────────────────────────────────────────────────
@app.route('/health')
def health():
    return {"status": "ok", "service": "FundIntel Server v15", "playwright": PLAYWRIGHT_AVAILABLE}

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
    print(f"{'='*50}\n  FundIntel v15 | port {port} | playwright={PLAYWRIGHT_AVAILABLE}\n{'='*50}")
    app.run(host='0.0.0.0', port=port, debug=False)
