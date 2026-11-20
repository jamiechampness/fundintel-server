"""Watchtower batch runner (POC). Works through a list of ISINs one at a time
in a background thread so the web request returns at once. Results are held
in memory (cleared on redeploy) - fine for the proof of concept."""
import json, os, threading, time
from datetime import date
from watchtower import (run_checks, summarise, exceptions, write_html, write_csv,
                        EXCEPTIONS)

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE = {"running": False, "started": None, "finished": None, "total": 0,
         "done": 0, "current": None, "rows": [], "errors": [], "stop": False}
_lock = threading.Lock()
PLATFORM = "Morningstar"
DOCS = ("KIID", "FACTSHEET", "REPORT_ANNUAL", "REPORT_INTERIM")


def load_isins():
    with open(os.path.join(_HERE, "watchtower_isins.json")) as f:
        return json.load(f)


def pick(n, isins):
    """n evenly spaced funds (a spread of umbrellas/classes), or all."""
    if not n or n >= len(isins):
        return list(isins)
    step = len(isins) / float(n)
    return [isins[int(i * step)] for i in range(n)]


def _check_one(item, fetch_source, fetch_platform, lookup):
    isin = item["isin"]
    src = fetch_source(isin)["dates"]
    msid = lookup(isin)
    plat = fetch_platform(msid)
    return [{"isin": isin, "name": item["name"], "doc_type": d,
             "source": src.get(d), PLATFORM: plat.get(d)} for d in DOCS]


def run(items, fetch_source, fetch_platform, lookup, pause=1.0):
    with _lock:
        STATE.update(running=True, started=time.time(), finished=None,
                     total=len(items), done=0, rows=[], errors=[], stop=False)
    for item in items:
        if STATE["stop"]:
            break
        STATE["current"] = item["isin"]
        try:
            STATE["rows"].extend(_check_one(item, fetch_source, fetch_platform, lookup))
        except Exception as e:      # one bad fund must not stop the run
            STATE["errors"].append({"isin": item["isin"], "name": item["name"],
                                    "error": str(e)[:300]})
        STATE["done"] += 1
        time.sleep(pause)
    STATE.update(running=False, finished=time.time(), current=None)


def start(n=None):
    if STATE["running"]:
        return False
    from watchtower_dimensional import fetch_source_dates
    from watchtower_morningstar import fetch_documents, isin_to_msid
    items = pick(n, load_isins())
    t = threading.Thread(target=run, args=(items, fetch_source_dates,
                                           fetch_documents, isin_to_msid), daemon=True)
    t.start()
    return True


def results():
    return run_checks(STATE["rows"], [PLATFORM], today=date.today())


def status():
    s = {k: STATE[k] for k in ("running", "total", "done", "current")}
    s["error_count"] = len(STATE["errors"])
    s["exceptions_so_far"] = len(exceptions(results()))
    return s
