"""Watchtower endpoints - ADDITIVE. Registered into the existing Flask app;
the charge scrapers and their routes are not touched."""
from flask import Blueprint, jsonify, request
from watchtower_morningstar import fetch_documents, isin_to_msid
from watchtower_dimensional import fetch_source_dates
from watchtower import compare

bp = Blueprint("watchtower", __name__)


@bp.route("/watchtower/health", methods=["GET"])
def wt_health():
    return jsonify({"ok": True, "module": "watchtower"})


@bp.route("/watchtower/morningstar", methods=["GET"])
def wt_morningstar():
    msid = (request.args.get("msid") or "").strip()
    if not msid.isalnum():
        return jsonify({"error": "msid required, e.g. 0P00015OCG"}), 400
    try:
        docs = fetch_documents(msid)
        return jsonify({"msid": msid,
                        "documents": {k: (v.isoformat() if v else None)
                                      for k, v in docs.items()}})
    except Exception as e:
        return jsonify({"msid": msid, "error": str(e)[:300]}), 502


@bp.route("/watchtower/source", methods=["GET"])
def wt_source():
    isin = (request.args.get("isin") or "").strip().upper()
    if len(isin) != 12 or not isin.isalnum():
        return jsonify({"error": "isin required, e.g. IE00BWGCG836"}), 400
    try:
        r = fetch_source_dates(isin)
        return jsonify({"isin": isin, "source": {k: (v.isoformat() if v else None)
                                                   for k, v in r["dates"].items()}})
    except Exception as e:
        return jsonify({"isin": isin, "error": str(e)[:600]}), 502


@bp.route("/watchtower/check", methods=["GET"])
def wt_check():
    """End to end for one ISIN: Dimensional (source) vs Morningstar."""
    isin = (request.args.get("isin") or "").strip().upper()
    if len(isin) != 12 or not isin.isalnum():
        return jsonify({"error": "isin required, e.g. IE00BWGCG836"}), 400
    out = {"isin": isin}
    try:
        src = fetch_source_dates(isin)["dates"]
    except Exception as e:
        return jsonify({"isin": isin, "stage": "source", "error": str(e)[:600]}), 502
    try:
        msid = request.args.get("msid") or isin_to_msid(isin)
        plat = fetch_documents(msid)
    except Exception as e:
        return jsonify({"isin": isin, "stage": "morningstar",
                        "source": {k: (v.isoformat() if v else None) for k, v in src.items()},
                        "error": str(e)[:600]}), 502
    out["msid"] = msid
    out["results"] = {}
    for dtype in ("KIID", "FACTSHEET", "REPORT_ANNUAL", "REPORT_INTERIM"):
        s, m = src.get(dtype), plat.get(dtype)
        out["results"][dtype] = {
            "dimensional": s.isoformat() if s else None,
            "morningstar": m.isoformat() if m else None,
            "status": compare(s, m)}
    return jsonify(out)


# ------------------------------------------------------------- batch runner
import html as _html
import os as _os
import tempfile as _tmp
from flask import Response
import watchtower_batch as _wb
from watchtower import write_html as _write_html, write_csv as _write_csv


@bp.route("/watchtower/batch/start", methods=["GET"])
def wt_batch_start():
    n = request.args.get("n", type=int)           # e.g. ?n=12 ; omit for all 215
    if not _wb.start(n):
        return jsonify({"started": False, "reason": "a run is already in progress",
                        "status": _wb.status()}), 409
    return jsonify({"started": True, "funds": n or "all",
                    "watch": "/watchtower/report"})


@bp.route("/watchtower/batch/status", methods=["GET"])
def wt_batch_status():
    return jsonify(_wb.status())


@bp.route("/watchtower/batch/stop", methods=["GET"])
def wt_batch_stop():
    _wb.STATE["stop"] = True
    return jsonify({"stopping": True})


def _tmp_render(fn, suffix):
    fd, path = _tmp.mkstemp(suffix=suffix)
    _os.close(fd)
    try:
        fn(path)
        with open(path, encoding="utf-8") as f:
            return f.read()
    finally:
        _os.remove(path)


@bp.route("/watchtower/report", methods=["GET"])
def wt_report():
    st = _wb.status()
    res = _wb.results()
    body = _tmp_render(lambda p: _write_html(res, p, "Dimensional (PDF dates)"), ".html")
    bar = ("<p style='font:14px system-ui;margin:12px 32px'><b>%s</b> &middot; %d of %d "
           "funds done &middot; %d could not be checked &middot; "
           "<a href='/watchtower/report.csv'>download CSV</a>%s</p>"
           % ("RUNNING (this page refreshes)" if st["running"] else "FINISHED",
              st["done"], st["total"], st["error_count"],
              " &middot; now: " + st["current"] if st["current"] else ""))
    errs = ""
    if _wb.STATE["errors"]:
        errs = ("<h2 style='font:600 18px system-ui;margin:24px 32px 6px'>Could not check (%d)</h2>"
                "<table style='font:13px system-ui;margin:0 32px'>" % len(_wb.STATE["errors"])
                + "".join("<tr><td style='padding:3px 12px'>%s<td style='padding:3px 12px'>%s"
                          "<td style='padding:3px 12px;color:#b91c1c'>%s</tr>"
                          % (e["isin"], _html.escape(e["name"]), _html.escape(e["error"]))
                          for e in _wb.STATE["errors"]) + "</table>")
    refresh = "<meta http-equiv='refresh' content='20'>" if st["running"] else ""
    page = body.replace("<h1>", refresh + bar + "<h1>", 1) + errs
    return Response(page, mimetype="text/html")


@bp.route("/watchtower/report.csv", methods=["GET"])
def wt_report_csv():
    data = _tmp_render(lambda p: _write_csv(_wb.results(), p), ".csv")
    return Response(data, mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=watchtower.csv"})
