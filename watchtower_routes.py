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
