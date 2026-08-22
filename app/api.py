"""JSON endpoints — implementation-plan §6.3, consumed by vanilla-JS fetch."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from report_parser import ReportParseError

from . import db, planning, push
from .merge import MergeError, MergeResult, ingest, resolve_proposals
from .models import PlannedAbsence, PushSubscription, Subject
from .services import coverage_for, dashboard_for, settings_for

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


@bp.post("/calendar/day")
@login_required
def calendar_day():
    """Tap a day: normal → holiday → swap → normal."""
    payload = request.get_json(silent=True) or {}
    try:
        on_date = date.fromisoformat(payload.get("date", ""))
    except ValueError:
        return jsonify(error="Bad date."), 400

    if on_date < date.today():
        return jsonify(error="Bunkr only plans forwards — past days can't change."), 400

    kind = payload.get("kind")
    if kind not in (None, "holiday", "swap"):
        return jsonify(error="Unknown day type."), 400

    swap_weekday = payload.get("swap_weekday")
    if kind == "swap":
        if not isinstance(swap_weekday, int) or not 0 <= swap_weekday <= 6:
            return jsonify(error="Pick which weekday's timetable runs."), 400

    planning.set_day(current_user, on_date, kind,
                     name=(payload.get("name") or None), swap_weekday=swap_weekday)
    return jsonify(ok=True, date=on_date.isoformat(), kind=kind,
                   swap_weekday=swap_weekday)


@bp.post("/absences")
@login_required
def add_absence():
    """Commit to missing a future date (whole day, or one subject on it)."""
    payload = request.get_json(silent=True) or {}
    try:
        on_date = date.fromisoformat(payload.get("date", ""))
    except ValueError:
        return jsonify(error="Bad date."), 400
    if on_date < date.today():
        return jsonify(error="That day has already happened."), 400

    subject_id = payload.get("subject_id")
    if subject_id is not None:
        subject = db.session.get(Subject, subject_id)
        if subject is None or subject.user_id != current_user.id:
            return jsonify(error="Unknown subject."), 404

    existing = (
        db.session.query(PlannedAbsence)
        .filter_by(user_id=current_user.id, on_date=on_date, subject_id=subject_id)
        .one_or_none()
    )
    if existing is None:
        db.session.add(PlannedAbsence(
            user_id=current_user.id, on_date=on_date, subject_id=subject_id,
            note=(payload.get("note") or None),
        ))
        db.session.commit()

    return jsonify(_wallet_payload())


@bp.delete("/absences/<int:absence_id>")
@login_required
def remove_absence(absence_id: int):
    row = db.session.get(PlannedAbsence, absence_id)
    if row is None or row.user_id != current_user.id:
        return jsonify(error="Not found."), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify(_wallet_payload())


@bp.post("/simulate")
@login_required
def simulate_plan():
    """Hypothetical absences — nothing is stored."""
    payload = request.get_json(silent=True) or {}
    extras = []
    for item in payload.get("absences") or []:
        try:
            extras.append((date.fromisoformat(item["date"]), item.get("subject_id")))
        except (KeyError, TypeError, ValueError):
            return jsonify(error="Malformed absence in the plan."), 400
    return jsonify(_wallet_payload(extras))


@bp.get("/push/key")
@login_required
def push_key():
    return jsonify(configured=push.is_configured(), public_key=push.public_key())


@bp.post("/push/subscribe")
@login_required
def push_subscribe():
    payload = request.get_json(silent=True) or {}
    endpoint = payload.get("endpoint")
    keys = payload.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        return jsonify(error="Incomplete subscription."), 400

    existing = (
        db.session.query(PushSubscription).filter_by(endpoint=endpoint).one_or_none()
    )
    if existing is not None:
        # Endpoints are unique per browser and get reused when someone signs in
        # as a different account on the same device. Reassign the row in place —
        # deleting and re-inserting trips the unique constraint on autoflush,
        # and leaving it would push one student's verdicts to another's phone.
        existing.user_id = current_user.id
        existing.p256dh = keys["p256dh"]
        existing.auth = keys["auth"]
        existing.user_agent = (payload.get("user_agent") or "")[:255]
    else:
        db.session.add(PushSubscription(
            user_id=current_user.id,
            endpoint=endpoint,
            p256dh=keys["p256dh"],
            auth=keys["auth"],
            user_agent=(payload.get("user_agent") or "")[:255],
        ))

    settings = settings_for(current_user)
    settings.notify_enabled = True
    db.session.commit()
    return jsonify(ok=True)


@bp.post("/push/unsubscribe")
@login_required
def push_unsubscribe():
    endpoint = (request.get_json(silent=True) or {}).get("endpoint")
    if endpoint:
        row = (
            db.session.query(PushSubscription)
            .filter_by(endpoint=endpoint, user_id=current_user.id)
            .one_or_none()
        )
        if row is not None:
            db.session.delete(row)

    remaining = (
        db.session.query(PushSubscription).filter_by(user_id=current_user.id).count()
    )
    if remaining == 0:
        settings_for(current_user).notify_enabled = False
    db.session.commit()
    return jsonify(ok=True)


@bp.post("/push/test")
@login_required
def push_test():
    if not push.is_configured():
        return jsonify(error="Notifications aren't configured on this server."), 400

    brief = planning.morning_brief(current_user)
    payload = push.brief_payload(brief) if brief else {
        "title": "Bunkr works",
        "body": "Nothing to report right now — you're all caught up.",
        "url": "/",
        "tag": "bunkr-test",
    }
    delivered = push.send_to_user(current_user, payload)
    if not delivered:
        return jsonify(error="No device accepted the notification."), 502
    return jsonify(ok=True, delivered=delivered)


def _wallet_payload(extras=None) -> dict:
    result = planning.simulate_for(current_user, extras or [])
    wallet = result.wallet
    return {
        "overall_budget": wallet.overall_budget,
        "overall_limit": wallet.overall_limit,
        "overall_remaining": wallet.overall_remaining,
        "overall_planned": wallet.overall_planned,
        "is_safe": result.is_safe,
        "breaks": result.breaks,
        "overall_breaks": result.overall_breaks,
        "subjects": [
            {
                "id": s.subject_id,
                "code": s.code,
                "limit": s.limit,
                "budget": s.budget,
                "remaining": s.remaining,
                "planned": s.planned_absences,
                "unreported": s.unreported,
                "projected_total": s.projected_total,
                "projected_pct": _pct(s.projected_worst_pct),
                "verdict": s.verdict.value,
            }
            for s in wallet.subjects
        ],
        "days": [
            {
                "date": d.on_date.isoformat(),
                "verdict": d.verdict.value,
                "reason": d.reason,
                "lectures": [
                    {"code": l.code, "start": l.start_time.isoformat()}
                    for l in d.lectures
                ],
            }
            for d in planning.day_strip(current_user, wallet=wallet)
        ],
    }


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
