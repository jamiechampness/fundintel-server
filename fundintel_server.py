#!/usr/bin/env python3
"""FundIntel Backend Server v23"""

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
    """Fetch page with Playwright and evaluate JS."""
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
        print(f"  PW error: {e}")
        return None

# ── Hargreaves Lansdown ────────────────────────────────────────
def fetch_hl(isin):
    """
    HL fund pages are static HTML.
    URL pattern: /funds/fund-discounts.../search-results/{letter}/{slug}
    Find the slug by fetching the ISIN search page with Playwright,
    then fetch the fund page with requests and parse static HTML.
    Charges table row: "Ongoing charge (OCF/TER): | 0.86%"
    """
    try:
        # HL SEDOL-based URL: /search-results/{SEDOL} goes directly to the fund page
        # From logs we know the search page returns all funds, not ISIN-filtered
        # But the SEDOL direct URL works: hl.co.uk/funds/fund-discounts.../search-results/{SEDOL}
        sedol = get_sedol(isin)
        fund_url = f"https://www.hl.co.uk/funds/fund-discounts,-prices--and--factsheets/search-results/{sedol}"
        print(f"  HL direct SEDOL URL: {fund_url}")

        # Fetch fund page with plain requests — it's static HTML
        r = SESSION.get(fund_url, timeout=15)
        text = r.text
        print(f"  HL page status: {r.status_code}, length: {len(text)}")

        # Debug: find what text surrounds 0.86 on this page
        idx = text.find('0.86')
        if idx > -1:
            print(f"  HL snippet around 0.86: {repr(text[max(0,idx-100):idx+50])}")
        idx2 = text.find('OCF')
        if idx2 > -1:
            print(f"  HL snippet around OCF: {repr(text[max(0,idx2-50):idx2+100])}")

        # Try multiple patterns
        ter = None
        for pattern in [
            r'Ongoing charge \(OCF/TER\)[^0-9]+([\d.]+)%',
            r'Ongoing charge \(OCF/TER\):[^0-9]+([\d.]+)',
            r'OCF/TER[^0-9]+([\d.]+)%',
            r'ongoing_charge["\s:]+([0-9.]+)',
            r'standard_ocf["\s:]+([0-9.]+)',
        ]:
            ter_m = re.search(pattern, text, re.IGNORECASE)
            if ter_m:
                ter = to_pct(ter_m.group(1))
                print(f"  HL matched pattern: {pattern}")
                break
        entry_m = re.search(r'Net initial charge[^0-9]+([\d.]+)%', text)
        entry = to_pct(entry_m.group(1)) if entry_m else None
        nav_m = re.search(r'Sell:([\d,]+\.?\d*p)', text)
        nav = nav_m.group(1) if nav_m else None

        print(f"  HL: ter={ter} entry={entry} nav={nav}")
        return {"ter": ter, "entryCharge": entry, "exitCharge": "0.00%", "perf1y": None, "srri": None, "nav": nav}
    except Exception as e:
        print(f"  HL error: {e}")
        return None

# ── Fidelity ──────────────────────────────────────────────────
def fetch_fidelity(isin):
    """Fidelity key-statistics is server-rendered — plain requests works."""
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
    OCF element: <div data-testid="ocfValue">0.86%</div>
    Use Playwright to render the page then extract via data-testid.
    """
    try:
        sedol = get_sedol(isin)
        print(f"  ii SEDOL: {sedol}")

        # ii URL pattern: ii.co.uk/funds/{slug}/{SEDOL}
        # Their fund search page doesn't filter by ISIN/SEDOL properly.
        # Use their site search to find the slug, or try known URL patterns.
        fund_url = None

        # Try 1: ii site search via requests - returns HTML with fund links
        try:
            r = SESSION.get(f"https://www.ii.co.uk/funds?search={isin}", timeout=10)
            m = re.search(r'href="(/funds/[^"]+/' + re.escape(sedol) + r')"', r.text, re.IGNORECASE)
            if m:
                fund_url = "https://www.ii.co.uk" + m.group(1)
                print(f"  ii found via requests search: {fund_url}")
        except Exception as e:
            print(f"  ii requests error: {e}")

        # Try 2: Playwright on ISIN search with longer wait
        if not fund_url:
            result = pw_js(f"https://www.ii.co.uk/funds?search={isin}", f"""
                () => {{
                    const sedol = '{sedol}';
                    const links = Array.from(document.querySelectorAll('a'));
                    for (const a of links) {{
                        if ((a.href||'').toUpperCase().includes(sedol.toUpperCase())) {{
                            return a.href;
                        }}
                    }}
                    // Debug - return all /funds/ links
                    return Array.from(document.querySelectorAll('a[href*="/funds/"]'))
                        .map(a => a.href).filter(h => h.split('/').length > 5).slice(0,5);
                }}
            """, wait_ms=10000)
            print(f"  ii Playwright result: {result}")
            if isinstance(result, str) and sedol.upper() in result.upper():
                fund_url = result

        print(f"  ii fund URL: {fund_url}")
        if not fund_url or not isinstance(fund_url, str):

            return None

        # Extract OCF using the data-testid="ocfValue" selector
        result = pw_js(fund_url, """
            () => {
                const el = document.querySelector('[data-testid="ocfValue"]');
                if (el) return { ocf: el.innerText.trim(), found: true };
                // Fallback: search body text
                const body = document.body.innerText;
                const m = body.match(/Ongoing Ch[^0-9]{0,40}([0-9]+[.][0-9]+)/);
                return { ocf: m ? m[1] : null, found: false };
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
    Trustnet URL: trustnet.com/factsheets/O/KV68/fund-name
    OCF is in static HTML table: <td>OCF</td><td>0.86%</td>
    Find the URL via search (Playwright to handle cookie consent),
    then fetch with requests and parse static HTML.
    """
    try:
        # Use Playwright to find the fund URL (handles cookie consent)
        search_url = f"https://www.trustnet.com/factsheets/f/search?search={isin}"
        # Single Playwright session: accept cookies then find links
        if PLAYWRIGHT_AVAILABLE:
            try:
                with sync_playwright() as p:
                    br = p.chromium.launch(headless=True)
                    ctx = br.new_context(user_agent=HEADERS["User-Agent"])
                    pg = ctx.new_page()
                    pg.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                    pg.wait_for_timeout(4000)
                    # Accept cookie consent
                    for sel in ['#onetrust-accept-btn-handler', 'button[class*="accept"]']:
                        try:
                            pg.click(sel, timeout=2000)
                            pg.wait_for_timeout(2000)
                            break
                        except: pass
                    # Now find factsheet links
                    result = pg.evaluate("""
                        () => {
                            const links = Array.from(document.querySelectorAll('a'))
                                .filter(a => (a.href||'').includes('/factsheets/') && 
                                            !(a.href||'').includes('search'))
                                .map(a => a.href);
                            return { links: links.slice(0,3), url: window.location.href };
                        }
                    """)
                    br.close()
            except Exception as te:
                print(f"  Trustnet PW error: {te}")
                result = None
        else:
            result = None

        print(f"  Trustnet search result: {result}")

        fund_url = None
        if result:
            if result.get('links'):
                fund_url = result['links'][0]
            elif result.get('url') and 'search' not in result.get('url',''):
                fund_url = result['url']

        if not fund_url:
            return None

        print(f"  Trustnet fund URL: {fund_url}")

        # Fetch with plain requests — Trustnet is static HTML
        r = SESSION.get(fund_url, timeout=15)
        text = r.text

        # Table: <td>OCF</td><td>0.86%</td>
        ter_m = re.search(r'<td[^>]*>OCF</td>\s*<td[^>]*>([\d.]+)%?</td>', text)
        if not ter_m:
            # Fallback: look for OCF near a percentage
            ter_m = re.search(r'>OCF<[^>]*>[^<]*<[^>]*>([\d.]+)%', text)
        ter = to_pct(ter_m.group(1)) if ter_m else None

        print(f"  Trustnet: ter={ter}")
        return {"ter": ter, "entryCharge": None, "exitCharge": None, "perf1y": None, "srri": None, "nav": None}
    except Exception as e:
        print(f"  Trustnet error: {e}")
        return None

# ── Morningstar ───────────────────────────────────────────────
def fetch_morningstar(isin):
    """
    Morningstar redirects to global.morningstar.com/en-gb/investments/funds/{id}/quote
    OCF element: <div class="sal-component-mip-fee-level__dp-value">0.860%</div>
    The fund ID in the redirect URL (0P00007VND) differs from the search result ID (F00000020Y).
    Use Playwright to follow the redirect and extract via class name.
    """
    try:
        # Get the UK snapshot URL which will redirect to global.morningstar.com
        search_r = SESSION.get(
            f"https://www.morningstar.co.uk/uk/funds/SecuritySearchResults.aspx?type=ALL&search={isin}",
            timeout=15
        )
        m = re.search(r'href="[^"]*snapshot\.aspx\?id=([^"&]+)"', search_r.text)
        if not m: return None
        fund_id = m.group(1)
        print(f"  Morningstar search ID: {fund_id}")

        snap_url = f"https://www.morningstar.co.uk/uk/funds/snapshot/snapshot.aspx?id={fund_id}"

        # Use Playwright to follow redirect and extract OCF by class name
        result = pw_js(snap_url, """
            () => {
                // Target class from dev tools: sal-component-mip-fee-level__dp-value
                const el = document.querySelector('.sal-component-mip-fee-level__dp-value');
                if (el) return { ocf: el.innerText.trim(), method: 'class', url: window.location.href };
                // Fallback: search all elements for percentage near "Ongoing"
                const body = document.body.innerText;
                const m = body.match(/Ongoing Charge[^0-9]+([0-9]+[.][0-9]+)/i);
                return { ocf: m ? m[1] : null, method: 'text', url: window.location.href };
            }
        """, wait_ms=15000)

        print(f"  Morningstar: {result}")

        if result and result.get('ocf'):
            try:
                f = float(str(result['ocf']).rstrip('%'))
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
    return jsonify({"status": "ok", "service": "FundIntel Server v25", "playwright": PLAYWRIGHT_AVAILABLE})

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
    print(f"{'='*50}\n  FundIntel v25 | port {port} | playwright={PLAYWRIGHT_AVAILABLE}\n{'='*50}")
    app.run(host='0.0.0.0', port=port, debug=False)
