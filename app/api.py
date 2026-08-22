"""JSON endpoints — implementation-plan §6.3, consumed by vanilla-JS fetch."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, time

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from report_parser import ReportParseError

from attendance_engine import DayVerdict

from . import db, planning
from .merge import MergeError, MergeResult, ingest, resolve_proposals
from .models import LectureInstance, LecturePrediction, PlannedAbsence, Subject
from .services import (
    UNKNOWN_STATUSES,
    apply_subject_edits,
    coverage_for,
    dashboard_for,
    settings_for,
    subject_limit,
)

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
    """Tap a day: normal → holiday → normal."""
    payload = request.get_json(silent=True) or {}
    try:
        on_date = date.fromisoformat(payload.get("date", ""))
    except ValueError:
        return jsonify(error="Bad date."), 400

    if on_date < date.today():
        return jsonify(error="Bunkr only plans forwards — past days can't change."), 400

    kind = payload.get("kind")
    if kind not in (None, "holiday"):
        return jsonify(error="Unknown day type."), 400

    planning.set_day(current_user, on_date, kind, name=(payload.get("name") or None))
    return jsonify(ok=True, date=on_date.isoformat(), kind=kind)


@bp.route("/lectures/<int:lecture_id>/prediction", methods=["PUT", "DELETE"])
@login_required
def lecture_prediction(lecture_id: int):
    """Say how you expect an unmarked lecture to resolve, or take it back."""
    lecture = db.session.get(LectureInstance, lecture_id)
    if lecture is None or lecture.user_id != current_user.id:
        return jsonify(error="Not found."), 404

    existing = (
        db.session.query(LecturePrediction)
        .filter_by(user_id=current_user.id, lecture_id=lecture_id)
        .one_or_none()
    )

    if request.method == "DELETE":
        if existing is not None:
            db.session.delete(existing)
            db.session.commit()
        return jsonify(ok=True, predicted=None)

    # Guessing at a lecture the college has already marked would be overwriting
    # fact with opinion, which is the one thing this feature must never do.
    if lecture.status not in UNKNOWN_STATUSES:
        return jsonify(error="The college has already marked that one."), 409

    predicted = (request.get_json(silent=True) or {}).get("predicted")
    if predicted not in ("P", "A"):
        return jsonify(error="Predict either P or A."), 400

    if existing is None:
        existing = LecturePrediction(user_id=current_user.id, lecture_id=lecture_id,
                                     predicted=predicted)
        db.session.add(existing)
    else:
        existing.predicted = predicted
    db.session.commit()
    return jsonify(ok=True, predicted=predicted)


@bp.get("/day/<on_date>")
@login_required
def day_sheet(on_date: str):
    """One day's lectures and what is already planned against them.

    Keyed by (subject, start) rather than by time alone: a timetable can run
    two subjects in the same slot, and two lectures of one subject in a day.
    """
    try:
        day = date.fromisoformat(on_date)
    except ValueError:
        return jsonify(error="Bad date."), 400

    holiday = next(
        (h for h in planning.holidays_for(current_user) if h.on_date == day), None
    )
    codes = {
        s.id: s.code
        for s in db.session.query(Subject).filter_by(user_id=current_user.id).all()
    }
    planned = (
        db.session.query(PlannedAbsence)
        .filter_by(user_id=current_user.id, on_date=day)
        .all()
    )
    whole_day = next(
        (p for p in planned if p.subject_id is None and p.start_time is None), None
    )

    def absence_for(subject_id, start):
        for row in planned:
            if row.subject_id != subject_id:
                continue
            if row.start_time is None or planning.same_minute(row.start_time, start):
                return row.id
        return None

    win = planning.windows(current_user)
    end = planning.semester_end(current_user)

    return jsonify(
        date=day.isoformat(),
        is_past=day < date.today(),
        in_semester=bool(end and day <= end),
        # False past the next checkpoint: the absence is real and will be
        # stored, it just doesn't spend from the budget on screen yet.
        in_horizon=bool(win.remaining_to and day <= win.remaining_to),
        horizon_to=win.remaining_to.isoformat() if win.remaining_to else None,
        holiday=({"name": holiday.name} if holiday else None),
        whole_day_absence_id=(whole_day.id if whole_day else None),
        lectures=[
            {
                "subject_id": o.slot.subject_id,
                "code": codes.get(o.slot.subject_id, "?"),
                "start": o.slot.start_time.isoformat(),
                "end": o.slot.end_time.isoformat(),
                "absence_id": absence_for(o.slot.subject_id, o.slot.start_time),
            }
            for o in planning.lectures_on(current_user, day)
        ],
        breaks=[
            {
                "start": b.start_time.isoformat(),
                "end": b.end_time.isoformat(),
                "label": b.label or "Break",
            }
            for b in planning.breaks_on(current_user, day)
        ],
        # The half-day the maths actually recommends, so the sheet can offer it
        # in one tap instead of leaving you to work out which boxes to tick.
        partial=_partial_payload(planning.day_plan_on(current_user, day)),
    )


def _partial_payload(plan):
    """The "leave after / arrive by" option, when there is one."""
    if plan is None or plan.verdict != DayVerdict.PARTIAL:
        return None
    return {
        "leave_after": plan.leave_after.isoformat() if plan.leave_after else None,
        "arrive_at": plan.arrive_at.isoformat() if plan.arrive_at else None,
        "reason": plan.reason,
        "freed_minutes": plan.freed_minutes,
        "covers_break": plan.covers_break,
        "skippable": [
            {"subject_id": l.subject_id, "start": l.start_time.isoformat()}
            for l in plan.skippable
        ],
    }


def _day_guard(payload):
    """The date every absence request needs, or the reason it can't be used."""
    try:
        on_date = date.fromisoformat(payload.get("date", ""))
    except ValueError:
        return None, (jsonify(error="Bad date."), 400)
    if on_date < date.today():
        return None, (jsonify(error="That day has already happened."), 400)

    # The other end was missing entirely, so an absence could be filed for any
    # date at all — including ones no semester will ever reach.
    end = planning.semester_end(current_user)
    if end and on_date > end:
        return None, (jsonify(error="That's after your semester ends."), 400)
    return on_date, None


def _ensure_absence(on_date, subject_id, raw_start, note=None):
    """Get or create one planned absence. Validates, but never commits.

    Left uncommitted so a batch — "leave after 12:00", which is four lectures
    and one decision — lands as one transaction rather than four races.
    """
    if subject_id is not None:
        subject = db.session.get(Subject, subject_id)
        if subject is None or subject.user_id != current_user.id:
            return None, (jsonify(error="Unknown subject."), 404)

    try:
        start_time = time.fromisoformat(raw_start) if raw_start else None
    except (TypeError, ValueError):
        return None, (jsonify(error="Bad time."), 400)

    # An absence against a lecture the timetable doesn't have would count zero
    # anyway; refusing it says so instead of silently storing a no-op.
    if subject_id is not None:
        lectures = planning.lectures_on(current_user, on_date)
        matches = [o for o in lectures if o.slot.subject_id == subject_id
                   and (start_time is None
                        or planning.same_minute(o.slot.start_time, start_time))]
        if not matches:
            return None, (jsonify(error="Your timetable has no such class that day."), 422)

    # SQLite treats NULLs as distinct in a unique index, so the constraint
    # alone would not stop two whole-day rows. The check stays.
    existing = (
        db.session.query(PlannedAbsence)
        .filter_by(user_id=current_user.id, on_date=on_date,
                   subject_id=subject_id, start_time=start_time)
        .one_or_none()
    )
    if existing is None:
        existing = PlannedAbsence(
            user_id=current_user.id, on_date=on_date, subject_id=subject_id,
            start_time=start_time, note=(note or None),
        )
        db.session.add(existing)
    return existing, None


@bp.post("/absences")
@login_required
def add_absence():
    """Commit to missing a future date (whole day, or one subject on it)."""
    payload = request.get_json(silent=True) or {}
    on_date, error = _day_guard(payload)
    if error:
        return error

    row, error = _ensure_absence(on_date, payload.get("subject_id"),
                                 payload.get("start"), payload.get("note"))
    if error:
        db.session.rollback()
        return error

    db.session.commit()
    # The id rides along so the caller can offer Undo without re-querying.
    return jsonify(dict(_wallet_payload(), absence_id=row.id))


@bp.post("/absences/batch")
@login_required
def add_absences():
    """Commit to missing several lectures of one day at once.

    "Leave after 12:00" is one decision, not four. Posting it as four requests
    raced them against each other and recomputed the whole wallet four times
    for an answer that only had to be worked out once.
    """
    payload = request.get_json(silent=True) or {}
    on_date, error = _day_guard(payload)
    if error:
        return error

    wanted = payload.get("lectures")
    if not isinstance(wanted, list) or not wanted:
        return jsonify(error="Nothing to plan."), 400

    ids = {}
    for item in wanted:
        if not isinstance(item, dict):
            db.session.rollback()
            return jsonify(error="Malformed lecture in the plan."), 400
        row, error = _ensure_absence(on_date, item.get("subject_id"), item.get("start"))
        if error:
            # All or nothing: a half-applied "leave after 12:00" is a plan the
            # user never made.
            db.session.rollback()
            return error
        db.session.flush()
        ids[f"{item.get('subject_id')}|{item.get('start')}"] = row.id

    db.session.commit()
    return jsonify(dict(_wallet_payload(), absence_ids=ids))


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
            start = item.get("start")
            extras.append((
                date.fromisoformat(item["date"]),
                item.get("subject_id"),
                time.fromisoformat(start) if start else None,
            ))
        except (KeyError, TypeError, ValueError):
            return jsonify(error="Malformed absence in the plan."), 400
    return jsonify(_wallet_payload(extras))


@bp.put("/timetable")
@login_required
def save_timetable():
    """Store the whole grid. The editor calls this every time you press Done.

    The grid is small and the client always holds all of it, so a whole-grid
    PUT is both simpler and safer than per-block patching: there is no way for
    the stored week to end up in a state the user never saw.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("blocks"), list):
        return jsonify(error="Malformed request."), 400

    entries = planning.entries_from(payload["blocks"])
    if not any(e.kind == "class" for e in entries):
        return jsonify(
            error="Add at least one class — an empty timetable can't project anything."
        ), 422

    try:
        planning.save_timetable(current_user, entries, source="manual")
    except ValueError as exc:
        return jsonify(error=str(exc)), 422

    return jsonify(ok=True, classes=sum(1 for e in entries if e.kind == "class"))


@bp.put("/subjects/<int:subject_id>")
@login_required
def update_subject(subject_id: int):
    """Save one subject from the card you edited it in.

    Same validation as the batch form on /subjects — both go through
    `apply_subject_edits` — so a per-card save can never accept something the
    whole-page save would reject.
    """
    subject = db.session.get(Subject, subject_id)
    if subject is None or subject.user_id != current_user.id:
        return jsonify(error="Unknown subject."), 404

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="Malformed request."), 400

    siblings = db.session.query(Subject).filter_by(user_id=current_user.id).all()
    values = {k: payload[k] for k in ("name", "code", "custom_limit") if k in payload}
    if not values:
        return jsonify(error="Nothing to save."), 400

    errors = apply_subject_edits(subject, siblings, values)
    if errors:
        db.session.rollback()
        return jsonify(errors=errors), 422

    db.session.commit()
    return jsonify(
        ok=True,
        subject={
            "id": subject.id,
            "name": subject.canonical_name,
            "code": subject.code,
            "custom_limit": subject.custom_limit,
            "limit": subject_limit(subject, settings_for(current_user)),
        },
    )


@bp.get("/subjects/<int:subject_id>/skip-ladder")
@login_required
def skip_ladder(subject_id: int):
    """What skipping 1, 2, 3... more lectures of this subject would cost.

    Subject and overall are reported separately because `build_wallet` caps
    every subject's budget at the overall one: a rung can be comfortable for
    DBMS and still break the 75% rule across everything.
    """
    subject = db.session.get(Subject, subject_id)
    if subject is None or subject.user_id != current_user.id:
        return jsonify(error="Unknown subject."), 404

    rungs = planning.skip_ladder_for(current_user, subject_id)
    dates = planning.upcoming_occurrences(current_user, subject_id,
                                          limit=len(rungs))
    win = planning.windows(current_user)

    # A rung reports what it newly breaks, so a subject that is *already* under
    # water would show "nothing new breaks" on every rung. Say which it is, or
    # the page reads as "still safe" while quoting 33%.
    current = planning.wallet_for(current_user).by_id().get(subject_id)
    already_broken = bool(current and current.verdict.value == "danger")

    return jsonify(
        subject={"id": subject.id, "code": subject.code,
                 "name": subject.canonical_name,
                 "already_broken": already_broken,
                 "current_pct": _pct(current.projected_worst_pct) if current else None,
                 "limit": current.limit if current else None},
        horizon={
            "to": win.remaining_to.isoformat() if win.remaining_to else None,
            "is_checkpoint": win.horizon_is_checkpoint,
            "label": win.checkpoint_label,
        },
        next_dates=[
            {"date": o.on_date.isoformat(),
             "start": o.slot.start_time.isoformat(),
             "end": o.slot.end_time.isoformat()}
            for o in dates
        ],
        ladder=[
            {
                "n": r.n,
                "subject_pct": _pct(r.subject_pct),
                "subject_verdict": r.subject_verdict.value,
                "subject_budget_left": r.subject_budget_left,
                "overall_pct": _pct(r.overall_pct),
                "overall_verdict": r.overall_verdict.value,
                "breaks": list(r.breaks),
                "is_safe": r.is_safe,
                "through_date": (dates[r.n - 1].on_date.isoformat()
                                 if r.n <= len(dates) else None),
            }
            for r in rungs
        ],
    )


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
            for d in planning.horizon_strip(current_user, wallet=wallet)
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
