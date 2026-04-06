#!/usr/bin/env python3
"""FundIntel Backend Server v55"""

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
        nav_m = re.search(r'bid price-divide[^>]*>\s*([\d,]+\.?\d*p)', text)
        nav = nav_m.group(1) if nav_m else None

        # SRRI and 1Y perf
        srri = None
        perf1y = None

        # Extract Charts & Performance URL from main page HTML
        charts_url = None
        charts_m = re.search(r'href="(https://www\.hl\.co\.uk[^"]*(?:charts|performance)[^"]*)"', text, re.IGNORECASE)
        if charts_m:
            charts_url = charts_m.group(1)
            print(f"  HL charts URL: {charts_url}")

        # SRRI — try costs-and-charges and key-features sub-pages
        for srri_suffix in ['/costs-and-charges', '/key-features']:
            try:
                cr = SESSION.get(fund_url + srri_suffix, timeout=10)
                if cr.status_code == 200:
                    ct = cr.text
                    srri_m = (re.search(r'class="[^"]*(?:selected|active|current)[^"]*"\s*>\s*([1-7])\s*<', ct, re.IGNORECASE) or
                              re.search(r'srri[^0-9]{0,50}([1-7])\b', ct, re.IGNORECASE) or
                              re.search(r'"srri"\s*:\s*([1-7])\b', ct, re.IGNORECASE))
                    if srri_m:
                        srri = int(srri_m.group(1)); print(f"  HL SRRI {srri_suffix}: {srri}"); break
                    else:
                        for kw in ['srri','lower risk','Risk and Reward']:
                            idx = ct.lower().find(kw.lower())
                            if idx > -1: print(f"  HL {srri_suffix} [{kw}]: {repr(ct[max(0,idx-20):idx+200])}"); break
            except Exception as e: print(f"  HL {srri_suffix} error: {e}")
            if srri: break

        # 1Y perf — Financial Express chartingTool page has a performance table
        # populated via SOAP calls. Use Playwright to wait for it to render.
        try:
            fe_tool_url = (f"https://webfund6.financialexpress.net/clients/Hargreaves/"
                           f"chartingTool.aspx?code={sedol}&CodeType=SEDOL&InstrType=F")
            fe_result = pw_js(fe_tool_url, r"""
                () => {
                    const body = document.body.innerText;
                    // Table headers: "3 months | 6 months | 1 year | 3 years | 5 years"
                    // Find "Cumulative performance" section then "1 year" column
                    const cumIdx = body.search(/Cumulative performance/i);
                    if (cumIdx > -1) {
                        const section = body.slice(cumIdx, cumIdx + 800);
                        // Match "1 year" row value — tab-separated table
                        const m = section.match(/1\s*year\s+([-+]?[\d.]+)/i);
                        if (m) return { perf: m[1], method: 'cumulative_table', section: section.slice(0,300) };
                    }
                    // Fallback: any percentage near "1 year"
                    const m2 = body.match(/1\s*year[^\d-+]{0,20}([-+]?[\d.]+)/i);
                    if (m2) return { perf: m2[1], method: 'body_match' };
                    return { perf: null, bodySnippet: body.slice(0, 500) };
                }
            """, wait_ms=12000)
            print(f"  HL FE tool result: {fe_result}")
            if fe_result and fe_result.get('perf'):
                try:
                    val = float(fe_result['perf'])
                    if -80 < val < 500:  # sanity check — not the 5yr 298%
                        perf1y = f"{val:+.1f}%"
                        print(f"  HL perf1y from FE: {perf1y}")
                except: pass
        except Exception as e: print(f"  HL FE error: {e}")

        # Fallback: Playwright with full network capture
        if not perf1y:
            try:
                perf_url = charts_url or (fund_url + '/charts')
                pr = SESSION.get(perf_url, timeout=10)
                print(f"  HL charts: status={pr.status_code}")
                if pr.status_code == 200:
                    pt = pr.text
                    perf_m = (re.search(r'1\s*yr[^%\d]{0,30}([-+]?[\d.]+)\s*%', pt, re.IGNORECASE) or
                              re.search(r'1\s*year[^%\d]{0,30}([-+]?[\d.]+)\s*%', pt, re.IGNORECASE))
                    if perf_m:
                        perf1y = f"{float(perf_m.group(1)):+.1f}%"; print(f"  HL perf1y: {perf1y}")
                    else:
                        try:
                            hl_api_urls = []
                            with sync_playwright() as p:
                                br = p.chromium.launch(headless=True)
                                ctx = br.new_context(user_agent=HEADERS["User-Agent"])
                                pg = ctx.new_page()
                                def on_req(req):
                                    u = req.url
                                    # Capture ALL non-asset requests to find the data API
                                    if not any(x in u for x in ['.js', '.css', '.woff', '.png', '.svg', '.ico', '.gif', 'gtm', 'analytics', 'hotjar', 'trustpilot']):
                                        hl_api_urls.append(u)
                                pg.on("request", on_req)
                                pg.goto(perf_url, wait_until="domcontentloaded", timeout=30000)
                                pg.wait_for_timeout(15000)
                                body_result = pg.evaluate(r"""
                                    () => {
                                        const body = document.body.innerText;
                                        const m = body.match(/1\s*yr[^\d%]{0,20}([-+]?[\d.]+)\s*%/i) ||
                                                  body.match(/1\s*year[^\d%]{0,20}([-+]?[\d.]+)\s*%/i);
                                        const tidx = body.search(/1\s*yr|1\s*year/i);
                                        const snippet = tidx>-1 ? body.slice(Math.max(0,tidx-30),tidx+400) : body.slice(0,200);
                                        return {perf: m?m[1]:null, snippet};
                                    }
                                """)
                                br.close()
                            print(f"  HL charts PW body: {body_result}")
                            print(f"  HL charts API URLs: {hl_api_urls[:30]}")
                            if body_result and body_result.get('perf'):
                                try: perf1y = f"{float(body_result['perf']):+.1f}%"
                                except: pass
                            if not perf1y:
                                for api_url in hl_api_urls:
                                    try:
                                        ar = SESSION.get(api_url, timeout=8)
                                        if ar.status_code == 200 and any(k in ar.text.lower() for k in ['perf','1yr','return','cumulative']):
                                            print(f"  HL API {api_url[-60:]}: {ar.text[:300]}")
                                            pm = re.search(r'(?:perf|return|1yr)[^-+\d]{0,30}([-+]?[\d.]+)', ar.text, re.IGNORECASE)
                                            if pm:
                                                try: perf1y = f"{float(pm.group(1)):+.1f}%"; break
                                                except: pass
                                    except: pass
                        except Exception as e2: print(f"  HL charts intercept error: {e2}")
            except Exception as e: print(f"  HL perf error: {e}")

        # Debug: show snippets around key terms so we can tune regexes
        for label in ['risk', 'SRRI', '1 year', 'performance', 'Sell:']:
            idx = text.lower().find(label.lower())
            if idx > -1:
                print(f"  HL [{label}] snippet: {repr(text[max(0,idx-20):idx+120])}")
        print(f"  HL: ter={ter} entry={entry} nav={nav} srri={srri} perf1y={perf1y}")
        return {"ter": ter, "entryCharge": entry, "exitCharge": "0.00%", "perf1y": perf1y, "srri": srri, "nav": nav}
    except Exception as e:
        print(f"  HL error: {e}")
        return None

# ── Fidelity ──────────────────────────────────────────────────
def fetch_fidelity(isin):
    """
    Fidelity key-statistics page is server-rendered.
    The canonical URL includes a slug: {ISIN}GBP-{fund-name-slug}
    We follow the redirect from the bare ISIN URL to get the full slug,
    then use that for the risk and performance sub-pages.
    Performance data is on /performance tab, SRRI on /risk tab.
    """
    try:
        # Fetch key-statistics — follow redirect to get canonical slug URL
        r = SESSION.get(
            f"https://www.fidelity.co.uk/factsheet-data/factsheet/{isin}/key-statistics",
            timeout=15, allow_redirects=True
        )
        text = r.text
        # r.url will now be the canonical URL e.g. /factsheet/GB00B0XWNG99GBP-abrdn-asia.../key-statistics
        canonical = r.url
        print(f"  Fid canonical URL: {canonical}")

        if r.status_code != 200:
            s = SESSION.get(f"https://www.fidelity.co.uk/search/?q={isin}", timeout=15)
            m = re.search(r'href="(/factsheet-data/factsheet/[^"]+/key-statistics)"', s.text)
            if not m: return None
            r = SESSION.get("https://www.fidelity.co.uk" + m.group(1), timeout=15, allow_redirects=True)
            text = r.text
            canonical = r.url

        # Extract base URL — ensure GBP suffix for sub-pages
        base_url = re.sub(r'/key-statistics$', '', canonical)
        if isin in base_url and isin + 'GBP' not in base_url:
            base_url = base_url.replace(isin, isin + 'GBP')
        print(f"  Fid base URL: {base_url}")

        ter_m = re.search(r'[Oo]ngoing charge \(%\)[^\d]+([\d.]+)', text)
        ter = to_pct(ter_m.group(1)) if ter_m else None
        entry_m = re.search(r'[Ff]und provider buy charge \(%\)[^\d]+([\d.]+)', text)
        entry = to_pct(entry_m.group(1)) if entry_m else "0.00%"
        # NAV: price appears as "421.45p" near top of key-stats page
        nav = None
        nav_m = (re.search(r'buy.sell price[^<]{0,300}?([\d,]+\.[\d]+p)', text, re.IGNORECASE | re.DOTALL) or
                 re.search(r'<h3[^>]*>\s*([\d,]+\.[\d]+p)\s*</h3>', text, re.IGNORECASE) or
                 re.search(r'>\s*(4\d\d\.\d+p)\s*<', text))
        if nav_m: nav = nav_m.group(1)
        print(f"  Fid NAV: {nav}")

        # SRRI from /risk sub-page — DOM approach (body has 1-7 scale labels, need highlighted one)
        srri = None
        try:
            srri_result = pw_js(f"{base_url}/risk", r"""
                async () => {
                    for (const t of document.querySelectorAll('li,button,a,[role="tab"]')) {
                        if (/risk.*rating|risk & rating/i.test(t.innerText||'')) {
                            t.click(); await new Promise(r=>setTimeout(r,3000)); break;
                        }
                    }
                    let srriDom = null;
                    for (const el of document.querySelectorAll('*')) {
                        const cls = (el.className||'').toString().toLowerCase();
                        const txt = (el.innerText||'').trim();
                        if (/srri|risk.?(box|number|cell|block|item)/i.test(cls) && /^[1-7]$/.test(txt)) {
                            srriDom = txt; break;
                        }
                    }
                    if (!srriDom) {
                        for (const el of document.querySelectorAll('[class*="active"],[class*="selected"],[class*="current"],[class*="highlight"]')) {
                            const txt = (el.innerText||'').trim();
                            if (/^[1-7]$/.test(txt)) { srriDom = txt; break; }
                        }
                    }
                    const body = document.body.innerText;
                    const tm = body.match(/([1-7])\s+out\s+of\s+7/i);
                    const si = body.search(/lower risk|srri|synthetic risk/i);
                    const snippet = si>-1 ? body.slice(Math.max(0,si-20),si+300) : '';
                    return {srri:srriDom||(tm?tm[1]:null), srriDom, snippet, bodyLen:body.length};
                }
            """, wait_ms=15000)
            print(f"  Fid risk PW result: {srri_result}")
            if srri_result and srri_result.get('srri'):
                srri = int(srri_result['srri'])
                print(f"  Fid SRRI: {srri}")
        except Exception as e: print(f"  Fid risk error: {e}")

        # 1Y performance from /performance sub-page
        perf1y = None
        try:
            # Performance page is JS-rendered (SPA) — use Playwright
            perf_result = pw_js(f"{base_url}/performance", r"""
                () => {
                    const body = document.body.innerText;
                    // Trailing returns table: find "1 Year" row
                    const tidx = body.search(/Trailing returns/i);
                    if (tidx > -1) {
                        const section = body.slice(tidx, tidx + 800);
                        const m = section.match(/1\s*Year[\s\S]{0,30}?([-+]?[\d.]+)/i);
                        if (m) return { perf: m[1], method: 'trailing', section: section.slice(0,200) };
                    }
                    // Fallback: any table row with "1 Year"
                    const m2 = body.match(/1\s*Year\s*[\n\r\s]+([-+]?[\d.]+)/i);
                    if (m2) return { perf: m2[1], method: 'fallback' };
                    return { perf: null, bodySnippet: body.slice(0, 400) };
                }
            """, wait_ms=10000)
            print(f"  Fid perf result: {perf_result}")
            if perf_result and perf_result.get('perf'):
                try: perf1y = f"{float(perf_result['perf']):+.1f}%"
                except: pass
        except Exception as e: print(f"  Fid perf error: {e}")

        print(f"  Fid: ter={ter} entry={entry} nav={nav} srri={srri} perf1y={perf1y}")
        return {"ter": ter, "entryCharge": entry, "exitCharge": "0.00%",
                "perf1y": perf1y, "srri": srri, "nav": nav}
    except Exception as e:
        print(f"  Fidelity error: {e}")
        return None

def fetch_ii(isin):
    """
    ii fund page URL: ii.co.uk/funds/{slug}/{SEDOL7}

    Strategy (v38):
    - 'Search' button is visible in nav from the start (before cookie consent).
    - Click it to open the search input, type the ISIN, wait for autocomplete.
    - Also try: the one input that exists (total:1) regardless of size filter.
    - Cookie consent 'Accept essential cookies only' doesn't dismiss — skip it.
    """
    try:
        sedol = get_sedol(isin)
        print(f"  ii SEDOL: {sedol}")

        if not PLAYWRIGHT_AVAILABLE:
            return None

        fund_url = None

        try:
            with sync_playwright() as p:
                br = p.chromium.launch(headless=True)
                ctx = br.new_context(user_agent=HEADERS["User-Agent"])
                pg = ctx.new_page()

                pg.goto("https://www.ii.co.uk/funds",
                        wait_until="domcontentloaded", timeout=30000)
                pg.wait_for_timeout(3000)

                # Click the nav "Search" button to open search UI
                clicked_search = pg.evaluate("""
                    () => {
                        const btns = Array.from(document.querySelectorAll('button'));
                        for (const b of btns) {
                            const t = (b.innerText || '').trim();
                            if (t === 'Search') { b.click(); return true; }
                        }
                        return false;
                    }
                """)
                print(f"  ii clicked Search button: {clicked_search}")
                pg.wait_for_timeout(1500)

                # Log all inputs now
                inputs = pg.evaluate("""
                    () => Array.from(document.querySelectorAll('input'))
                        .map(i => {
                            const r = i.getBoundingClientRect();
                            return {type:i.type, placeholder:i.placeholder,
                                    id:i.id, name:i.name,
                                    w:Math.round(r.width), h:Math.round(r.height)};
                        })
                """)
                print(f"  ii inputs after Search click: {inputs}")

                # Try every non-hidden, non-checkbox input regardless of size
                filled = False
                fill_sel = None
                for inp in inputs:
                    if inp.get('type') in ('checkbox', 'hidden', 'radio'):
                        continue
                    # Build selector
                    if inp.get('id'):
                        sel = f"#{inp['id']}"
                    elif inp.get('name') and inp['name'] not in ('vendor-search-handler',):
                        sel = f"input[name='{inp['name']}']"
                    elif inp.get('placeholder') and 'vendor' not in inp.get('placeholder','').lower():
                        sel = f"input[placeholder='{inp['placeholder']}']"
                    else:
                        continue
                    try:
                        pg.fill(sel, isin, timeout=2000)
                        filled = True
                        fill_sel = sel
                        print(f"  ii filled: {sel} (w={inp.get('w')}, h={inp.get('h')})")
                        break
                    except Exception as e:
                        print(f"  ii fill {sel} failed: {e}")

                # If still not filled, try filling the first input outright
                if not filled:
                    try:
                        pg.evaluate(f"""
                            () => {{
                                const inputs = Array.from(document.querySelectorAll('input'));
                                const t = inputs.find(i => i.type !== 'checkbox' && i.type !== 'hidden');
                                if (t) {{ t.value = '{isin}'; t.dispatchEvent(new Event('input', {{bubbles:true}})); }}
                            }}
                        """)
                        filled = True
                        print("  ii filled via JS value assignment")
                    except Exception as e:
                        print(f"  ii JS fill error: {e}")

                if filled:
                    pg.wait_for_timeout(2500)

                    # Check for autocomplete suggestions containing SEDOL
                    auto_url = pg.evaluate(
                        r"(s) => { for (const a of document.querySelectorAll('a')) { if ((a.href||'').toUpperCase().includes(s.toUpperCase())) return a.href; } return null; }",
                        sedol
                    )
                    if auto_url:
                        fund_url = auto_url
                        print(f"  ii autocomplete: {fund_url}")

                    if not fund_url:
                        # Log what appeared after typing
                        suggestions = pg.evaluate("""
                            () => Array.from(document.querySelectorAll('a[href*="/funds/"]'))
                                .map(a => a.href).filter(h => h.split('/').length > 5).slice(0,5)
                        """)
                        print(f"  ii fund links after typing: {suggestions}")

                        pg.keyboard.press("Enter")
                        pg.wait_for_timeout(5000)
                        current = pg.url
                        print(f"  ii URL after enter: {current}")
                        if '/funds/' in current and current != "https://www.ii.co.uk/funds":
                            fund_url = current
                        else:
                            dom_url = pg.evaluate(
                                r"(s) => { for (const a of document.querySelectorAll('a')) { if ((a.href||'').toUpperCase().includes(s.toUpperCase())) return a.href; } return null; }",
                                sedol
                            )
                            if dom_url:
                                fund_url = dom_url
                                print(f"  ii DOM link: {dom_url}")
                            else:
                                # Log all fund links visible for debug
                                all_links = pg.evaluate("""
                                    () => Array.from(document.querySelectorAll('a[href*="/funds/"]'))
                                        .map(a=>a.href).filter(h=>h.split('/').length>5).slice(0,8)
                                """)
                                print(f"  ii all fund links after enter: {all_links}")
                else:
                    print("  ii: could not fill any input")

                br.close()
        except Exception as e:
            print(f"  ii Playwright error: {e}")

        print(f"  ii final fund URL: {fund_url}")
        if not fund_url:
            return None

        result = pw_js(fund_url, r"""
            () => {
                const get = id => { const e = document.querySelector('[data-testid="' + id + '"]'); return e ? (e.innerText||'').trim() : null; };
                const ocf  = get('ocfValue');
                // NAV: nav-price-value testId gives "421.45p" directly
                const nav  = get('nav-price-value') || get('bidPrice') || get('navPrice');
                // 1Y perf: price-change-container contains "Chg\n\n24.02%"
                const perfEl = document.querySelector('[data-testid="price-change-container"]');
                let perf1y = null;
                if (perfEl) {
                    const pm = (perfEl.innerText||'').match(/([-+]?[\d.]+)\s*%/);
                    if (pm) perf1y = pm[1] + '%';
                }
                const srriEl = document.querySelector('[data-testid="srriValue"], [data-testid="sri"]');
                const srri = srriEl ? (srriEl.innerText||'').trim() : null;
                const body = document.body.innerText;
                const testIds = Array.from(document.querySelectorAll('[data-testid]'))
                    .map(e => e.getAttribute('data-testid') + '=' + (e.innerText||'').trim().slice(0,80))
                    .filter(s => s.length > 1).slice(0, 35);
                if (ocf) return { ocf, nav, srri, perf1y, entry: null, method: 'testid', testIds };
                const m = body.match(/Ongoing [Cc]harg[^0-9]{0,60}?([\d]+[.][\d]+)\s*%/);
                return { ocf: m ? m[1]+'%' : null, nav, srri, perf1y, entry: null,
                    method: m ? 'body-text' : 'not-found',
                    testIds, bodyStart: body.slice(0, 400) };
            }
        """, wait_ms=8000)

        print(f"  ii full result: {result}")
        if result and result.get('ocf'):
            srri_val = None
            if result.get('srri'):
                try: srri_val = int(str(result['srri']).strip())
                except: pass
            perf = result.get('perf1y')
            if perf:
                try: perf = f"{float(str(perf).rstrip('%')):+.1f}%"
                except: pass
            return {"ter": to_pct(result['ocf']),
                    "entryCharge": to_pct(result.get('entry')) or "0.00%",
                    "exitCharge": "0.00%",
                    "perf1y": perf,
                    "srri": srri_val,
                    "nav": result.get('nav')}
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
    return jsonify({"status": "ok", "service": "FundIntel Server v55", "playwright": PLAYWRIGHT_AVAILABLE})

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
    print(f"{'='*50}\n  FundIntel v55 | port {port} | playwright={PLAYWRIGHT_AVAILABLE}\n{'='*50}")
    app.run(host='0.0.0.0', port=port, debug=False)
