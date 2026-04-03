#!/usr/bin/env python3
"""FundIntel Backend Server v30"""

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
        # HL: The SEDOL URL is a search results PAGE, not the fund's own page.
        # The fund's own page uses a named slug: /search-results/{letter}/{fund-name}
        # Use Playwright to load the SEDOL search result and navigate to the fund page.
        sedol = get_sedol(isin)
        search_url = f"https://www.hl.co.uk/funds/fund-discounts,-prices--and--factsheets/search-results/{sedol}"
        
        fund_url = None
        if PLAYWRIGHT_AVAILABLE:
            try:
                with sync_playwright() as p:
                    br = p.chromium.launch(headless=True)
                    ctx = br.new_context(user_agent=HEADERS["User-Agent"])
                    pg = ctx.new_page()
                    pg.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                    pg.wait_for_timeout(5000)
                    # Find the fund name link in the search results table
                    # These are links with the pattern /search-results/{letter}/{slug}
                    fund_url = pg.evaluate("""
                        () => {
                            const links = Array.from(document.querySelectorAll('a[href*="search-results"]'));
                            const skip = ['invest','key-features','charts','research','costs',
                                         'fund-analysis','?','#','start='];
                            for (const a of links) {
                                const h = a.href || '';
                                const parts = h.split('/');
                                // Named slug URLs have format: .../search-results/a/fund-name
                                // They have 8+ parts and the 7th part is a single letter
                                if (parts.length >= 8 && 
                                    parts[parts.length-2].length === 1 &&
                                    !skip.some(s => h.includes(s))) {
                                    return h;
                                }
                            }
                            return null;
                        }
                    """)
                    print(f"  HL fund URL from search: {fund_url}")
                    br.close()
            except Exception as e:
                print(f"  HL search error: {e}")

        if not fund_url:
            return None

        # Fetch the actual fund page — this has the charges table in static HTML
        r = SESSION.get(fund_url, timeout=15)
        text = r.text
        print(f"  HL fund page status: {r.status_code}, length: {len(text)}")

        # From logs: "Ongoing charge (OCF/TER)</span>\r\n            </th>\r\n        <td>\r\n            0.86%"
        # Strategy: find "Ongoing charge (OCF/TER)" then find the NEXT <td> content
        # Use a tight window (500 chars) to avoid matching performance figures far away
        ter = None
        idx = text.find('Ongoing charge (OCF/TER)')
        if idx == -1:
            idx = text.lower().find('ongoing charge (ocf/ter)')
        if idx > -1:
            window = text[idx:idx+500]
            # % is HTML-encoded as &#37; in HL's HTML
            # Match either 0.86% or 0.86&#37;
            td_m = re.search(r'<td[^>]*>\s*([\d.]+)(?:%|&#37;)', window)
            if td_m:
                ter = to_pct(td_m.group(1))
                print(f"  HL OCF found: {ter}")
            else:
                print(f"  HL window: {repr(window[:300])}")
        else:
            print("  HL: OCF label not found in page")
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

    Strategy (v30):
    1. Use Playwright to load ii's search page and INTERCEPT the XHR/fetch calls
       that the page makes — ii fires an API call to retrieve search results as JSON.
       We capture that response directly rather than scraping the DOM.
    2. Fallback: try known REST endpoints with varied path patterns.
    3. Fallback: Playwright on the fund page directly constructed from SEDOL,
       iterating plausible slug patterns.
    """
    try:
        sedol = get_sedol(isin)
        print(f"  ii SEDOL: {sedol}")

        fund_url = None

        # ── Strategy 1: Intercept XHR on the search page ──────────────────
        if PLAYWRIGHT_AVAILABLE and not fund_url:
            try:
                captured = []
                with sync_playwright() as p:
                    br = p.chromium.launch(headless=True)
                    ctx = br.new_context(user_agent=HEADERS["User-Agent"])
                    pg = ctx.new_page()

                    # Intercept all API responses that look like search results
                    def handle_response(response):
                        url = response.url
                        if any(kw in url for kw in ['search', 'instrument', 'fund', 'lookup']):
                            try:
                                body = response.text()
                                if sedol.upper() in body.upper() or isin.upper() in body.upper():
                                    captured.append({'url': url, 'body': body})
                                    print(f"  ii XHR captured: {url}")
                            except:
                                pass

                    pg.on("response", handle_response)
                    pg.goto(f"https://www.ii.co.uk/funds?search={isin}",
                            wait_until="domcontentloaded", timeout=30000)
                    pg.wait_for_timeout(8000)

                    # Also try triggering the search explicitly if there's an input
                    try:
                        pg.fill('input[type="search"], input[placeholder*="search" i], input[name*="search" i]', isin)
                        pg.wait_for_timeout(4000)
                    except:
                        pass

                    # Check if any links contain SEDOL after full load
                    dom_result = pg.evaluate(f"""
                        () => {{
                            const sedol = '{sedol}';
                            const links = Array.from(document.querySelectorAll('a'));
                            for (const a of links) {{
                                if ((a.href||'').toUpperCase().includes(sedol.toUpperCase())) {{
                                    return a.href;
                                }}
                            }}
                            return null;
                        }}
                    """)
                    if dom_result:
                        fund_url = dom_result
                        print(f"  ii DOM link found: {fund_url}")

                    br.close()

                # Parse captured XHR for fund URL
                if not fund_url and captured:
                    for cap in captured:
                        m = re.search(r'/funds/([a-z0-9-]+)/' + re.escape(sedol), cap['body'], re.IGNORECASE)
                        if m:
                            fund_url = "https://www.ii.co.uk" + m.group(0)
                            print(f"  ii URL from XHR: {fund_url}")
                            break
                        # Also look for slug pattern near SEDOL in JSON
                        m2 = re.search(r'"slug"\s*:\s*"([^"]+)"', cap['body'])
                        if m2:
                            fund_url = f"https://www.ii.co.uk/funds/{m2.group(1)}/{sedol}"
                            print(f"  ii URL from slug: {fund_url}")
                            break

            except Exception as e:
                print(f"  ii XHR intercept error: {e}")

        # ── Strategy 2: REST API endpoint variants ─────────────────────────
        if not fund_url:
            api_candidates = [
                f"https://www.ii.co.uk/ii-spa/api/instruments/search?term={isin}&type=FUNDS",
                f"https://www.ii.co.uk/ii-spa/api/instruments/search?term={sedol}&type=FUNDS",
                f"https://www.ii.co.uk/ii-spa/api/instruments/search?q={isin}",
                f"https://www.ii.co.uk/ii-spa/api/funds/search?term={isin}",
                f"https://www.ii.co.uk/ii-spa/api/search?q={isin}&asset_type=fund",
                f"https://www.ii.co.uk/api/instruments?isin={isin}",
                f"https://www.ii.co.uk/api/funds?isin={isin}",
            ]
            for api_url in api_candidates:
                try:
                    api_r = SESSION.get(
                        api_url,
                        headers={**HEADERS, "Accept": "application/json", "Referer": "https://www.ii.co.uk/"},
                        timeout=8
                    )
                    print(f"  ii API {api_url[-70:]}: {api_r.status_code}")
                    if api_r.status_code == 200:
                        body = api_r.text
                        print(f"  ii API body[:200]: {body[:200]}")
                        # Look for fund page URL pattern
                        m = re.search(r'/funds/([a-z0-9-]+)/' + re.escape(sedol), body, re.IGNORECASE)
                        if m:
                            fund_url = "https://www.ii.co.uk" + m.group(0)
                            print(f"  ii URL from API: {fund_url}")
                            break
                        # Look for slug
                        m2 = re.search(r'"(?:slug|urlSlug|url_slug)"\s*:\s*"([^"]+)"', body)
                        if m2:
                            fund_url = f"https://www.ii.co.uk/funds/{m2.group(1)}/{sedol}"
                            print(f"  ii URL from slug field: {fund_url}")
                            break
                except Exception as e:
                    print(f"  ii API error for {api_url[-40:]}: {e}")
                if fund_url:
                    break

        # ── Strategy 3: Playwright — load fund page directly by SEDOL ──────
        # ii fund pages always contain the SEDOL in the URL. The slug varies
        # but we can try loading /funds/{anything}/{SEDOL} via a redirect-aware
        # Playwright fetch, using Google to find the exact URL first.
        if not fund_url and PLAYWRIGHT_AVAILABLE:
            try:
                with sync_playwright() as p:
                    br = p.chromium.launch(headless=True)
                    ctx = br.new_context(user_agent=HEADERS["User-Agent"])
                    pg = ctx.new_page()
                    # Use ii's own search with SEDOL (sometimes works better than ISIN)
                    pg.goto(f"https://www.ii.co.uk/funds?search={sedol}",
                            wait_until="domcontentloaded", timeout=30000)
                    pg.wait_for_timeout(8000)
                    result = pg.evaluate(f"""
                        () => {{
                            const sedol = '{sedol}';
                            const links = Array.from(document.querySelectorAll('a'));
                            for (const a of links) {{
                                if ((a.href||'').toUpperCase().includes(sedol.toUpperCase())) {{
                                    return a.href;
                                }}
                            }}
                            // Return all fund-looking links for debug
                            return Array.from(document.querySelectorAll('a'))
                                .filter(a => (a.href||'').includes('/funds/') && a.href.split('/').length > 5)
                                .map(a => a.href).slice(0, 10);
                        }}
                    """)
                    print(f"  ii SEDOL search result: {result}")
                    if isinstance(result, str) and sedol.upper() in result.upper():
                        fund_url = result
                    br.close()
            except Exception as e:
                print(f"  ii SEDOL search error: {e}")

        print(f"  ii final fund URL: {fund_url}")
        if not fund_url or not isinstance(fund_url, str):
            return None

        # ── Extract OCF from fund page ─────────────────────────────────────
        result = pw_js(fund_url, r"""
            () => {
                // Primary: data-testid attribute
                const el = document.querySelector('[data-testid="ocfValue"]');
                if (el) return { ocf: el.innerText.trim(), method: 'testid' };

                // Secondary: any element containing a % value near fee/charge class
                const all = Array.from(document.querySelectorAll('*'));
                for (const el of all) {
                    const t = el.innerText || '';
                    if (t.length < 50 && /^[\d.]+%$/.test(t.trim())) {
                        const parent = el.closest('[class*="fee"],[class*="charge"],[class*="cost"],[class*="ocf"]');
                        if (parent) return { ocf: t.trim(), method: 'class-match' };
                    }
                }

                // Tertiary: body text regex
                const body = document.body.innerText;
                const m = body.match(/Ongoing [Cc]harg[^0-9]{0,60}?([\d]+[.][\d]+)\s*%/);
                if (m) return { ocf: m[1] + '%', method: 'body-text' };

                // Debug: return page title and first 500 chars
                return { ocf: null, method: 'not-found',
                    title: document.title,
                    bodyStart: document.body.innerText.slice(0, 500) };
            }
        """, wait_ms=8000)

        print(f"  ii OCF result: {result}")
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
        # Try direct requests first - Trustnet search may return static HTML
        try:
            r_trust = SESSION.get(
                f"https://www.trustnet.com/factsheets/f/search?search={isin}",
                timeout=15
            )
            print(f"  Trustnet requests status: {r_trust.status_code}")
            trust_text = r_trust.text
            # Look for factsheet links in the HTML
            trust_links = re.findall(r'href="(/factsheets/[^"]+)"', trust_text)
            trust_links = [l for l in trust_links if 'search' not in l and len(l.split('/')) >= 4]
            print(f"  Trustnet static links: {trust_links[:3]}")
            if trust_links:
                result = {'links': ['https://www.trustnet.com' + trust_links[0]], 'url': ''}
            else:
                result = None
        except Exception as e:
            print(f"  Trustnet requests error: {e}")
            result = None
        
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
    return jsonify({"status": "ok", "service": "FundIntel Server v30", "playwright": PLAYWRIGHT_AVAILABLE})

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
    print(f"{'='*50}\n  FundIntel v30 | port {port} | playwright={PLAYWRIGHT_AVAILABLE}\n{'='*50}")
    app.run(host='0.0.0.0', port=port, debug=False)
