"""Watchtower endpoints - ADDITIVE. Registered into the existing Flask app;
the charge scrapers and their routes are not touched."""
from flask import Blueprint, jsonify, request
from watchtower_morningstar import fetch_documents

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
