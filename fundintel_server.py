#!/usr/bin/env python3
"""FundIntel Backend Server v16"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import requests, re, time, os, json

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
    if not PLAYWRIGHT_AVAILABLE:
        return None, None
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(headless=True)
            ctx = br.new_context(user_agent=HEADERS["User-Agent"])
            pg = ctx.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=30000)
            pg.wait_for_timeout(wait_ms)
            html = pg.content()
            final_url = pg.url
            br.close()
            return html, final_url
    except Exception as e:
        print(f"  PW error: {e}")
        return None, None

def pw_evaluate(url, js_expr, wait_ms=6000, wait_until="domcontentloaded"):
    """Load page and evaluate JavaScript expression to extract data."""
    if not PLAYWRIGHT_AVAILABLE:
        return None
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(headless=True)
            ctx = br.new_context(user_agent=HEADERS["User-Agent"])
            pg = ctx.new_page()
            pg.goto(url, wait_until=wait_until, timeout=30000)
            pg.wait_for_timeout(wait_ms)
            result = pg.evaluate(js_expr)
            br.close()
            return result
    except Exception as e:
        print(f"  PW evaluate error: {e}")
        return None

# ── Hargreaves Lansdown ────────────────────────────────────────
def fetch_hl(isin):
    """
    HL: Use JS evaluation to find the fund link from search results page,
    then evaluate the fund page to extract OCF from the rendered table.
    Use domcontentloaded (not networkidle) as HL never fully settles.
    """
    try:
        sedol = get_sedol(isin)
        print(f"  HL SEDOL: {sedol}")

        # Use JS evaluate on search page to find fund link
        search_url = f"https://www.hl.co.uk/funds/fund-discounts,-prices--and--factsheets/search-results?ISINsearch={isin}"
        
        fund_url = None
        if PLAYWRIGHT_AVAILABLE:
            try:
                with sync_playwright() as p:
                    br = p.chromium.launch(headless=True)
                    ctx = br.new_context(user_agent=HEADERS["User-Agent"])
                    pg = ctx.new_page()
                    pg.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                    pg.wait_for_timeout(5000)
                    # Extract fund link via JS
                    link = pg.evaluate("""
                        () => {
                            const links = Array.from(document.querySelectorAll('a[href*="fund-discounts"]'));
                            const skip = ['/invest','/key-features','/charts','/research','/costs','/fund-analysis'];
                            for (const a of links) {
                                const h = a.href;
                                if (!skip.some(s => h.includes(s)) && h.split('/').length >= 8) {
                                    return h;
                                }
                            }
                            return null;
                        }
                    """)
                    print(f"  HL fund link from JS: {link}")
                    if link:
                        fund_url = link
                    br.close()
            except Exception as e:
                print(f"  HL search error: {e}")

        if not fund_url:
            print(f"  HL: no fund URL found")
            return None

        print(f"  HL fund URL: {fund_url}")

        # Use Playwright to evaluate JavaScript variables on the fund page
        # HL stores charges in fd.standard_ocf, fd.initial_charge etc.
        result = pw_evaluate(fund_url, """
            () => {
                try {
                    // Try to find OCF in various JS variables HL uses
                    let ocf = null;
                    let entry = null;
                    
                    // Method 1: fd object
                    if (typeof fd !== 'undefined') {
                        ocf = fd.standard_ocf || fd.ongoing_charge || fd.ocf;
                        entry = fd.initial_charge || fd.net_initial_charge;
                    }
                    
                    // Method 2: Look in page text for the rendered value
                    const tables = document.querySelectorAll('table');
                    for (const table of tables) {
                        const text = table.innerText;
                        if (text.includes('OCF') || text.includes('Ongoing charge')) {
                            const rows = table.querySelectorAll('tr');
                            for (const row of rows) {
                                const cells = row.querySelectorAll('td, th');
                                for (let i = 0; i < cells.length; i++) {
                                    if (cells[i].innerText.includes('OCF/TER') || cells[i].innerText.includes('Ongoing charge (OCF')) {
                                        if (cells[i+1]) {
                                            const val = cells[i+1].innerText.trim();
                                            if (val.match(/^\\d+\\.\\d+%?$/)) {
                                                ocf = val;
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    
                    // Method 3: Look in all text nodes
                    if (!ocf) {
                        const bodyText = document.body.innerText;
                        const ocfMatch = bodyText.match(/OCF\\/TER[\\s\\S]{1,100}?(\\d+\\.\\d+)%/);
                        if (ocfMatch) ocf = ocfMatch[1];
                    }
                    
                    return {ocf: ocf, entry: entry, bodySnippet: document.body.innerText.substring(0, 2000)};
                } catch(e) {
                    return {error: e.toString()};
                }
            }
        """, wait_ms=5000)

        if result:
            print(f"  HL JS result: ocf={result.get('ocf')} entry={result.get('entry')}")
            # Print snippet to see what's rendered
            snippet = result.get('bodySnippet', '')
            ocf_pos = snippet.find('OCF')
            if ocf_pos > -1:
                print(f"  HL body snippet near OCF: {snippet[ocf_pos:ocf_pos+200]}")
            
            ter = to_pct(result.get('ocf'))
            entry = to_pct(result.get('entry'))
            return {"ter": ter, "entryCharge": entry, "exitCharge": "0.00%", "perf1y": None, "srri": None, "nav": None}

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
    ii: Use Playwright to search by ISIN and find the fund page.
    Known URL pattern: ii.co.uk/funds/{slug}/{SEDOL}
    """
    try:
        sedol = get_sedol(isin)
        print(f"  ii SEDOL: {sedol}")

        # Use Playwright to search and find fund link containing our SEDOL
        search_url = f"https://www.ii.co.uk/funds?search={isin}"
        result = pw_evaluate(search_url, f"""
            () => {{
                const sedol = '{sedol}';
                // Find all links on page
                const links = Array.from(document.querySelectorAll('a'));
                // Find one containing our SEDOL
                for (const link of links) {{
                    if (link.href && link.href.includes(sedol)) {{
                        return link.href;
                    }}
                }}
                // Debug: return all fund-like links
                const fundLinks = links
                    .filter(l => l.href && l.href.includes('/funds/'))
                    .map(l => l.href)
                    .slice(0, 5);
                return fundLinks;
            }}
        """, wait_ms=8000)

        print(f"  ii search result: {result}")

        fund_url = None
        if isinstance(result, str) and sedol in result:
            fund_url = result
        elif isinstance(result, list):
            for url in result:
                if sedol in url:
                    fund_url = url
                    break

        if not fund_url:
            print(f"  ii: no fund URL found for SEDOL {sedol}")
            return None

        print(f"  ii fund URL: {fund_url}")

        result = pw_evaluate(fund_url, """
            () => {
                const bodyText = document.body.innerText;
                const ocfMatch = bodyText.match(/[Oo]ngoing [Cc]harge[\\s\\S]{1,100}?(\\d+\\.\\d+)%/);
                const idx = bodyText.indexOf('Ongoing');
                return {
                    ocf: ocfMatch ? ocfMatch[1] : null,
                    snippet: idx > -1 ? bodyText.substring(idx, idx+300) : bodyText.substring(0, 300)
                };
            }
        """, wait_ms=8000)

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
    Trustnet: Dismiss cookie consent banner first, then find fund page.
    """
    try:
        if not PLAYWRIGHT_AVAILABLE:
            return None

        with sync_playwright() as p:
            br = p.chromium.launch(headless=True)
            ctx = br.new_context(user_agent=HEADERS["User-Agent"])
            pg = ctx.new_page()
            
            url = f"https://www.trustnet.com/factsheets/f/search?search={isin}"
            pg.goto(url, wait_until="networkidle", timeout=30000)
            pg.wait_for_timeout(3000)

            # Dismiss cookie consent if present
            for selector in ['button:has-text("Accept")', 'button:has-text("Accept All")',
                            'button:has-text("I Accept")', '#acceptAll', '.accept-btn',
                            'button:has-text("OK")', 'button:has-text("Agree")']:
                try:
                    btn = pg.query_selector(selector)
                    if btn:
                        btn.click()
                        pg.wait_for_timeout(1000)
                        print(f"  Trustnet: dismissed cookie consent ({selector})")
                        break
                except:
                    pass

            pg.wait_for_timeout(3000)

            # Find factsheet links (not search page links)
            links = pg.evaluate("""
                () => {
                    const links = Array.from(document.querySelectorAll('a[href*="factsheets"]'));
                    return links
                        .map(l => l.href)
                        .filter(h => h && !h.includes('search') && h.length > 40)
                        .slice(0, 5);
                }
            """)
            print(f"  Trustnet links: {links}")

            if not links:
                # Try navigating directly if redirected
                current_url = pg.url
                print(f"  Trustnet current URL: {current_url}")
                if 'factsheets' in current_url and 'search' not in current_url:
                    result = pg.evaluate("""
                        () => {
                            const bodyText = document.body.innerText;
                            const m = bodyText.match(/[Oo]ngoing [Cc]harge[\\s\\S]{1,50}?(\\d+\\.\\d+)%/);
                            return { ocf: m ? m[1] : null };
                        }
                    """)
                    br.close()
                    if result and result.get('ocf'):
                        return {"ter": to_pct(result['ocf']), "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
                br.close()
                return None

            fund_url = links[0]
            print(f"  Trustnet fund URL: {fund_url}")
            pg.goto(fund_url, wait_until="networkidle", timeout=30000)
            pg.wait_for_timeout(4000)

            result = pg.evaluate("""
                () => {
                    const bodyText = document.body.innerText;
                    const m = bodyText.match(/[Oo]ngoing [Cc]harge[\\s\\S]{1,100}?(\\d+\\.\\d+)%/);
                    const idx = bodyText.indexOf('Ongoing');
                    return {
                        ocf: m ? m[1] : null,
                        snippet: idx > -1 ? bodyText.substring(idx, idx+200) : ''
                    };
                }
            """)
            br.close()
            print(f"  Trustnet result: {result}")
            if result and result.get('ocf'):
                return {"ter": to_pct(result['ocf']), "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

        return None
    except Exception as e:
        print(f"  Trustnet error: {e}")
        return None

# ── Morningstar ───────────────────────────────────────────────
def fetch_morningstar(isin):
    """Use JS evaluation on Morningstar snapshot page."""
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
        
        result = pw_evaluate(snap_url, """
            () => {
                const bodyText = document.body.innerText;
                const ocfMatch = bodyText.match(/[Oo]ngoing [Cc]harge[\\s\\S]{1,50}?(\\d+\\.\\d+)/);
                const snippet = bodyText.substring(0, 2000);
                return { ocf: ocfMatch ? ocfMatch[1] : null, snippet: snippet };
            }
        """, wait_ms=8000)

        print(f"  Morningstar result: ocf={result.get('ocf') if result else None}")
        if result and result.get('snippet'):
            print(f"  MS snippet: {result['snippet'][:300]}")

        if result and result.get('ocf'):
            return {"ter": to_pct(result['ocf']), "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}

        return None
    except Exception as e:
        print(f"  Morningstar error: {e}")
        return None

# ── Routes ────────────────────────────────────────────────────
@app.route('/health')
def health():
    return {"status": "ok", "service": "FundIntel Server v18", "playwright": PLAYWRIGHT_AVAILABLE}

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
    print(f"{'='*50}\n  FundIntel v18 | port {port} | playwright={PLAYWRIGHT_AVAILABLE}\n{'='*50}")
    app.run(host='0.0.0.0', port=port, debug=False)
