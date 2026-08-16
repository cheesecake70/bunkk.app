"""Core blueprint — Phase 0 keeps this minimal: health check + version."""
from flask import Blueprint, jsonify

bp = Blueprint("core", __name__)


@bp.get("/healthz")
def healthz():
    return jsonify(status="ok", app="bunkmate", phase=0)
