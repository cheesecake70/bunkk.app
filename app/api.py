"""JSON endpoints — implementation-plan §6.3, consumed by vanilla-JS fetch."""
from __future__ import annotations

from dataclasses import asdict

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from report_parser import ReportParseError

from .merge import MergeError, MergeResult, ingest, resolve_proposals
from .services import coverage_for, dashboard_for

bp = Blueprint("api", __name__, url_prefix="/api")

MAX_PDF_BYTES = 5 * 1024 * 1024


@bp.post("/reports")
@login_required
def upload_report():
    file = request.files.get("report")
    if file is None or not file.filename:
        return jsonify(error="Choose a PDF to upload."), 400
    if not file.filename.lower().endswith(".pdf"):
        return jsonify(error="That isn't a PDF. Export the detailed report as PDF."), 400

    data = file.read()
    if not data:
        return jsonify(error="That file is empty."), 400
    if len(data) > MAX_PDF_BYTES:
        return jsonify(error="That file is too large to be an attendance report."), 400

    try:
        result = ingest(current_user, data, file.filename)
    except (ReportParseError, MergeError) as exc:
        # Parser errors carry user-facing wording by design (report_parser.types).
        return jsonify(error=str(exc)), 422

    return jsonify(_payload(result))


@bp.post("/reports/<int:snapshot_id>/resolve")
@login_required
def resolve_report(snapshot_id: int):
    decisions = (request.get_json(silent=True) or {}).get("decisions") or {}
    if not isinstance(decisions, dict):
        return jsonify(error="Malformed answer."), 400

    try:
        result = resolve_proposals(current_user, snapshot_id, decisions)
    except (ReportParseError, MergeError) as exc:
        return jsonify(error=str(exc)), 422

    return jsonify(_payload(result))


@bp.get("/dashboard")
@login_required
def dashboard():
    dash = dashboard_for(current_user)
    coverage = coverage_for(current_user)
    return jsonify(
        overall={
            "limit": dash.overall.limit,
            "present": dash.overall.counts.present,
            "absent": dash.overall.counts.absent,
            "pending": dash.overall.counts.unknown,
            "worst_pct": _pct(dash.overall.worst_pct),
            "official_pct": _pct(dash.overall.official_pct),
            "best_pct": _pct(dash.overall.best_pct),
            "can_miss": dash.overall.can_miss,
            "verdict": dash.overall.verdict.value,
        },
        subjects=[
            {
                "id": s.subject_id,
                "code": s.code,
                "name": s.canonical_name,
                "type": s.lecture_type,
                "limit": s.limit,
                "present": s.counts.present,
                "absent": s.counts.absent,
                "pending": s.counts.unknown,
                "worst_pct": _pct(s.worst_pct),
                "official_pct": _pct(s.official_pct),
                "best_pct": _pct(s.best_pct),
                "can_miss": s.can_miss_effective,
                "recover_needed": s.recover_needed,
                "verdict": s.verdict.value,
            }
            for s in dash.subjects
        ],
        coverage={
            "gaps": [str(g) for g in coverage.gaps],
            "stale_days": coverage.stale_days,
            "is_stale": coverage.is_stale,
            "pending_count": coverage.pending_count,
            "suggested_export": str(coverage.suggested_export)
            if coverage.suggested_export else None,
        },
    )


def _pct(value) -> float | None:
    return round(float(value), 1) if value is not None else None


def _change(change) -> dict:
    """Flask's JSON provider handles `date` but not `time`, so both are made
    explicit here rather than relying on `asdict` to produce something
    serialisable."""
    return {
        "subject_code": change.subject_code,
        "on_date": change.on_date.isoformat(),
        "start_time": change.start_time.isoformat(),
        "from_status": change.from_status,
        "to_status": change.to_status,
    }


def _payload(result: MergeResult) -> dict:
    return {
        "status": result.status,
        "snapshot_id": result.snapshot_id,
        "period_start": result.period_start.isoformat() if result.period_start else None,
        "period_end": result.period_end.isoformat() if result.period_end else None,
        "added": result.added,
        "unchanged": result.unchanged,
        "updated": result.updated,
        "resolved_pending": result.resolved_pending,
        "changes": [_change(c) for c in result.changes],
        "vanished": [_change(c) for c in result.vanished],
        "new_subjects": result.new_subjects,
        "pct_moves": result.pct_moves,
        "proposals": [asdict(p) for p in result.proposals],
    }
