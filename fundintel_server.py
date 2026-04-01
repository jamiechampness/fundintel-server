#!/usr/bin/env python3
"""FundIntel Backend Server v21"""

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

def pw_js(url, js, wait_ms=6000):
    """Fetch page and evaluate JS — uses domcontentloaded to avoid timeout."""
    if not PLAYWRIGHT_AVAILABLE:
        return None
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(headless=True)
            ctx = br.new_context(user_agent=HEADERS["User-Agent"])
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(wait_ms)
            result = pg.evaluate(js)
            br.close()
            return result
    except Exception as e:
        print(f"  PW JS error: {e}")
        return None

# ── Hargreaves Lansdown ────────────────────────────────────────
def fetch_hl(isin):
    """
    HL: Find fund slug via Playwright search, then fetch static HTML for OCF.
    Page shows: "Ongoing charge (OCF/TER): 0.86%" in a table.
    """
    try:
        search_url = f"https://www.hl.co.uk/funds/fund-discounts,-prices--and--factsheets/search-results?ISINsearch={isin}"
        fund_url = pw_js(search_url, """
            () => {
                const links = Array.from(document.querySelectorAll('a[href*="fund-discounts"]'));
                const skip = ['invest','key-features','charts','research','costs','fund-analysis','?tab','ISINsearch'];
                for (const a of links) {
                    const h = a.href || '';
                    if (h.split('/').length >= 8 && !skip.some(s => h.includes(s))) {
                        return h;
                    }
                }
                return null;
            }
        """, wait_ms=6000)
        print(f"  HL fund URL: {fund_url}")
        if not fund_url: return None

        r = SESSION.get(fund_url, timeout=15)
        text = r.text
        ter_m = re.search(r'Ongoing charge \(OCF/TER\)[^0-9]+([\d.]+)%', text)
        ter = to_pct(ter_m.group(1)) if ter_m else None
        entry_m = re.search(r'Net initial charge[^0-9]+([\d.]+)%', text)
        entry = to_pct(entry_m.group(1)) if entry_m else None
        nav_m = re.search(r'Sell:([\d,]+\.?\d*p)', text)
        nav = nav_m.group(1) if nav_m else None
        print(f"  HL: ter={ter} entry={entry}")
        return {"ter": ter, "entryCharge": entry, "exitCharge": "0.00%", "perf1y": None, "srri": None, "nav": nav}
    except Exception as e:
        print(f"  HL error: {e}")
        return None

# ── Fidelity ──────────────────────────────────────────────────
def fetch_fidelity(isin):
    """Fidelity key-statistics page is server-rendered — plain requests works."""
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
    ii URL: ii.co.uk/funds/{slug}/{SEDOL}
    Page shows "Ongoing Ch... 0.86%" in summary grid (JS rendered).
    """
    try:
        sedol = get_sedol(isin)
        print(f"  ii SEDOL: {sedol}")

        # Find fund URL via search
        search_url = f"https://www.ii.co.uk/funds?search={isin}"
        fund_url = pw_js(search_url, f"""
            () => {{
                const sedol = '{sedol}';
                const links = Array.from(document.querySelectorAll('a'));
                for (const a of links) {{
                    if (a.href && a.href.toUpperCase().includes(sedol.toUpperCase())) {{
                        return a.href;
                    }}
                }}
                return null;
            }}
        """, wait_ms=8000)

        print(f"  ii fund URL: {fund_url}")
        if not fund_url:
            return None

        # Extract OCF from rendered page
        result = pw_js(fund_url, """
            () => {
                const bodyText = document.body.innerText;
                const m = bodyText.match(/Ongoing Ch[^\\n\\r]{0,20}[\\n\\r\\s]+([\\.\\d]+)/);
                const idx = bodyText.search(/Ongoing/i);
                return {
                    ocf: m ? m[1] : null,
                    snippet: idx > -1 ? bodyText.substring(idx, idx+80) : ''
                };
            }
        """, wait_ms=6000)

        print(f"  ii result: {result}")
        if result and result.get('ocf'):
            return {"ter": to_pct(result['ocf']), "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
        return None
    except Exception as e:
        print(f"  ii error: {e}")
        return None

# ── Trustnet ──────────────────────────────────────────────────
def fetch_trustnet(isin):
    """
    Trustnet: URL is /factsheets/O/KV68/fund-name
    Page shows "OCF: 0.86%" in Unit Information section.
    Need to dismiss cookie consent first.
    """
    try:
        search_url = f"https://www.trustnet.com/factsheets/f/search?search={isin}"
        result = pw_js(search_url, """
            () => {
                // Dismiss cookie consent
                const btn = document.querySelector('#onetrust-accept-btn-handler');
                if (btn) btn.click();
                // Find factsheet links (not search links)
                const links = Array.from(document.querySelectorAll('a[href*="/factsheets/"]'))
                    .filter(a => !a.href.includes('search'))
                    .map(a => a.href);
                return { links: links.slice(0,3), url: window.location.href };
            }
        """, wait_ms=8000)

        print(f"  Trustnet: {result}")
        if not result or not result.get('links'): return None

        fund_url = result['links'][0]
        print(f"  Trustnet fund URL: {fund_url}")

        ocf = pw_js(fund_url, """
            () => {
                const btn = document.querySelector('#onetrust-accept-btn-handler');
                if (btn) btn.click();
                const bodyText = document.body.innerText;
                const m = bodyText.match(/\\bOCF[:\\s]+([\\d.]+)/);
                const idx = bodyText.indexOf('OCF');
                return { ocf: m ? m[1] : null, snippet: idx > -1 ? bodyText.substring(idx, idx+30) : '' };
            }
        """, wait_ms=5000)

        print(f"  Trustnet OCF: {ocf}")
        if ocf and ocf.get('ocf'):
            return {"ter": to_pct(ocf['ocf']), "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
        return None
    except Exception as e:
        print(f"  Trustnet error: {e}")
        return None

# ── Morningstar ───────────────────────────────────────────────
def fetch_morningstar(isin):
    """
    Morningstar redirects to global.morningstar.com
    OCF shown as "Ongoing Charge" in the overview bar.
    """
    try:
        r = SESSION.get(
            f"https://www.morningstar.co.uk/uk/funds/SecuritySearchResults.aspx?type=ALL&search={isin}",
            timeout=15
        )
        m = re.search(r'href="[^"]*snapshot\.aspx\?id=([^"&]+)"', r.text)
        if not m: return None
        fund_id = m.group(1)
        print(f"  Morningstar ID: {fund_id}")

        snap_url = f"https://www.morningstar.co.uk/uk/funds/snapshot/snapshot.aspx?id={fund_id}"
        result = pw_js(snap_url, """
            () => {
                const bodyText = document.body.innerText;
                const m = bodyText.match(/Ongoing Charge[^\\d]*([\\d.]+)/i);
                const idx = bodyText.search(/Ongoing/i);
                return {
                    ocf: m ? m[1] : null,
                    url: window.location.href,
                    snippet: idx > -1 ? bodyText.substring(idx, idx+100) : bodyText.substring(0,200)
                };
            }
        """, wait_ms=10000)

        print(f"  Morningstar: url={result.get('url') if result else None} ocf={result.get('ocf') if result else None}")
        if result and result.get('snippet'):
            print(f"  MS snippet: {result['snippet'][:150]}")

        if result and result.get('ocf'):
            try:
                f = float(result['ocf'])
                if 0.05 < f < 5.0:
                    return {"ter": f"{f:.2f}%", "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
            except: pass
        return None
    except Exception as e:
        print(f"  Morningstar error: {e}")
        return None

# ── Routes ────────────────────────────────────────────────────
@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "service": "FundIntel Server v21", "playwright": PLAYWRIGHT_AVAILABLE})

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

    print(f"\nDone. {len(results)} platforms.\n")
    return jsonify(results)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f"{'='*50}\n  FundIntel v21 | port {port} | playwright={PLAYWRIGHT_AVAILABLE}\n{'='*50}")
    app.run(host='0.0.0.0', port=port, debug=False)
