"""
Watchtower (working name) - document-currency check. POC.

ADDITIVE module: does not touch the FundIntel charge scrapers.

Logic
-----
The asset manager's own website is the SOURCE OF TRUTH (e.g. Dimensional).
Platforms (Morningstar, Fidelity Advisor) host copies of documents. For every
ISIN x document type x platform we compare the platform's document date with
the source date:

    platform == source      -> IN_DATE
    platform <  source      -> STALE      (exception)
    platform missing        -> MISSING    (exception)
    platform >  source      -> AHEAD      (oddity - source scrape may be wrong)
    source missing          -> NO_SOURCE  (cannot judge against source)

A secondary fixed-rule sanity check is also run against each date
(KIID, factsheet, reports) so we can spot cases where BOTH source and
platform are old, or where the source scrape itself looks wrong.
"""
import csv
import re
import html
from datetime import date, datetime

DOC_TYPES = ["KIID", "FACTSHEET", "REPORT_ANNUAL", "REPORT_INTERIM"]

IN_DATE, STALE, MISSING, AHEAD, NO_SOURCE = (
    "IN_DATE", "STALE", "MISSING", "AHEAD", "NO_SOURCE")
EXCEPTIONS = {STALE, MISSING, AHEAD}


def parse_date(v):
    if v in (None, "", "-"):
        return None
    if isinstance(v, date):
        return v
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(str(v).strip(), fmt).date()
        except ValueError:
            pass
    return None


def compare(source, platform):
    """Primary check: platform date vs the asset manager's date."""
    s, p = parse_date(source), parse_date(platform)
    if s is None:
        return NO_SOURCE
    if p is None:
        return MISSING
    if p == s:
        return IN_DATE
    return STALE if p < s else AHEAD


def fixed_rule_ok(doc_type, d, today=None):
    """Secondary sanity check. Configurable - thresholds need validating.
    KIID: Feb of the current year or later.
    FACTSHEET: monthly -> dated this month or last month (month-end lag).
    REPORT: annual + interim (May / Nov) -> not older than the most recent
            May-1 / Nov-1 cutoff.  (Assumption - confirm with Steve.)
    Returns True / False / None (no date)."""
    d = parse_date(d)
    if d is None:
        return None
    today = today or date.today()
    if doc_type == "KIID":
        return d >= date(today.year, 2, 1)
    if doc_type == "FACTSHEET":
        # monthly, published with a lag: month-end date within the last 60 days
        return (today - d).days <= 60
    if doc_type in ("REPORT_ANNUAL", "REPORT_INTERIM"):
        # d is the PERIOD END. Annual = 30 Nov, interim = 31 May.
        # Expected once ~120 days have passed since that period end.
        m, day = (11, 30) if doc_type == "REPORT_ANNUAL" else (5, 31)
        cands = [date(y, m, day) for y in (today.year - 1, today.year)]
        due = [c for c in cands if (today - c).days >= 120]
        return d >= max(due) if due else True
    return None



# ------------------------------------------------------- date extraction
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}


def _dmy(d, m, y):
    return date(int(y), int(m), int(d))


def extract_doc_date(text, doc_type):
    """Pull the date a document states about itself from its first-page text.
    KIID/KID : 'accurate as at DD-MM-YYYY'
    FACTSHEET: 'AS OF 30 SEPTEMBER 2026'
    REPORTS  : 'financial year ended 30 November 2025' (period end)
               'period ended 31 May 2026' for interim
    Returns a date or None (None => flag for manual review, never guess)."""
    t = " ".join(text.split())
    if doc_type == "KIID":
        m = re.search(r"accurate as at\s+(\d{2})\s*[-/.]\s*(\d{2})\s*[-/.]\s*(\d{4})", t, re.I)
        return _dmy(m[1], m[2], m[3]) if m else None
    pat = r"(\d{1,2})\s+(%s)\s+(\d{4})" % "|".join(_MONTHS)
    if doc_type == "FACTSHEET":
        m = re.search(r"as of\s+" + pat, t, re.I)
    else:
        m = re.search(r"(?:year|period|months?)\s+ended\s+" + pat, t, re.I)
    return _dmy(m[1], _MONTHS[m[2].lower()], m[3]) if m else None


def run_checks(rows, platforms, today=None):
    """rows: list of dicts
         {isin, name, doc_type, source: date, <platform>: date, ...}
       Returns list of result dicts, one per isin x doc_type x platform."""
    out = []
    for r in rows:
        src_rule = fixed_rule_ok(r["doc_type"], r.get("source"), today)
        for p in platforms:
            status = compare(r.get("source"), r.get(p))
            out.append({
                "isin": r["isin"], "name": r.get("name", ""),
                "doc_type": r["doc_type"], "platform": p,
                "source_date": parse_date(r.get("source")),
                "platform_date": parse_date(r.get(p)),
                "status": status,
                "platform_rule_ok": fixed_rule_ok(r["doc_type"], r.get(p), today),
                "source_rule_ok": src_rule,
            })
    return out


def summarise(results):
    """In/out-of-date percentages overall and by platform x doc type."""
    def pct(rs):
        judged = [x for x in rs if x["status"] != NO_SOURCE]
        n = len(judged)
        ok = sum(1 for x in judged if x["status"] == IN_DATE)
        return {"checked": n, "in_date": ok,
                "out_of_date": n - ok,
                "pct_in_date": round(100 * ok / n, 1) if n else None}
    summ = {"overall": pct(results)}
    keys = sorted({(x["platform"], x["doc_type"]) for x in results})
    for k in keys:
        summ["%s | %s" % k] = pct([x for x in results
                                    if (x["platform"], x["doc_type"]) == k])
    return summ


def exceptions(results):
    return [x for x in results if x["status"] in EXCEPTIONS]


def write_csv(results, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ISIN", "Fund", "Document", "Platform", "Source date",
                    "Platform date", "Status", "Days behind"])
        for x in results:
            behind = ""
            if x["source_date"] and x["platform_date"]:
                behind = (x["source_date"] - x["platform_date"]).days
            w.writerow([x["isin"], x["name"], x["doc_type"], x["platform"],
                        x["source_date"] or "", x["platform_date"] or "",
                        x["status"], behind])


def write_html(results, path, source_name="Asset manager website"):
    summ = summarise(results)
    exc = exceptions(results)
    colour = {STALE: "#b45309", MISSING: "#b91c1c", AHEAD: "#6d28d9"}
    h = ["<!doctype html><meta charset=utf-8><title>Document currency report</title>",
         "<style>body{font:14px system-ui;margin:32px;color:#1a2b3c}"
         "table{border-collapse:collapse;margin:12px 0}"
         "td,th{border-bottom:1px solid #ddd;padding:6px 12px;text-align:left}"
         "th{font-size:12px;text-transform:uppercase;color:#667}</style>",
         "<h1>Document currency report</h1>",
         "<p>Source of truth: %s &middot; generated %s</p>"
         % (html.escape(source_name), date.today())]
    o = summ["overall"]
    h.append("<h2>Overall: %s%% in date</h2><p>%d checks &middot; %d in date &middot; "
             "%d exceptions</p>" % (o["pct_in_date"], o["checked"],
                                    o["in_date"], o["out_of_date"]))
    h.append("<h2>By platform and document</h2><table><tr><th>Platform | Doc"
             "<th>Checked<th>In date<th>% in date</tr>")
    for k, v in summ.items():
        if k != "overall":
            h.append("<tr><td>%s<td>%d<td>%d<td>%s%%</tr>"
                     % (html.escape(k), v["checked"], v["in_date"], v["pct_in_date"]))
    h.append("</table><h2>Exceptions (%d)</h2><table><tr><th>ISIN<th>Fund<th>Doc"
             "<th>Platform<th>Source<th>Platform<th>Status</tr>" % len(exc))
    for x in sorted(exc, key=lambda x: (x["status"], x["isin"])):
        h.append("<tr><td>%s<td>%s<td>%s<td>%s<td>%s<td>%s<td style='color:%s;"
                 "font-weight:600'>%s</tr>" % (
                     x["isin"], html.escape(x["name"]), x["doc_type"], x["platform"],
                     x["source_date"] or "-", x["platform_date"] or "missing",
                     colour[x["status"]], x["status"]))
    h.append("</table>")
    with open(path, "w") as f:
        f.write("\n".join(h))


# ---------------------------------------------------------------- sample/test
SAMPLE = [
    # in date everywhere
    {"isin": "IE00B0000001", "name": "Sample Global Equity", "doc_type": "KIID",
     "source": "2026-02-10", "Morningstar": "2026-02-10", "Fidelity Advisor": "2026-02-10"},
    # Scenario from the meeting: platform holds Feb 26, manager has Aug 26
    {"isin": "IE00B0000002", "name": "Sample Value Fund", "doc_type": "KIID",
     "source": "2026-08-14", "Morningstar": "2026-08-14", "Fidelity Advisor": "2026-02-10"},
    # Missing on one platform
    {"isin": "IE00B0000003", "name": "Sample Small Cap", "doc_type": "FACTSHEET",
     "source": "2026-10-31", "Morningstar": "2026-10-31", "Fidelity Advisor": None},
    # Platform ahead of source (oddity)
    {"isin": "IE00B0000004", "name": "Sample Bond Fund", "doc_type": "FACTSHEET",
     "source": "2026-09-30", "Morningstar": "2026-10-31", "Fidelity Advisor": "2026-09-30"},
    # Both stale: source itself looks old
    {"isin": "IE00B0000005", "name": "Sample EM Fund", "doc_type": "REPORT_ANNUAL",
     "source": "2025-11-30", "Morningstar": "2025-11-30", "Fidelity Advisor": "2025-11-30"},
    # No source date
    {"isin": "IE00B0000006", "name": "Sample Multi-Asset", "doc_type": "KIID",
     "source": None, "Morningstar": "2026-02-10", "Fidelity Advisor": "2026-02-10"},
]

if __name__ == "__main__":
    platforms = ["Morningstar", "Fidelity Advisor"]
    res = run_checks(SAMPLE, platforms, today=date(2026, 11, 20))
    for k, v in summarise(res).items():
        print(k, v)
    print()
    for x in exceptions(res):
        print(x["isin"], x["doc_type"], x["platform"], x["status"])
    write_csv(res, "/mnt/user-data/outputs/watchtower_sample.csv")
    write_html(res, "/mnt/user-data/outputs/watchtower_sample.html", "Sample data")
