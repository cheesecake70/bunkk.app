"""JSON endpoints — implementation-plan §6.3, consumed by vanilla-JS fetch."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, time, timedelta
from functools import wraps

from flask import Blueprint, jsonify, request
from flask_limiter.util import get_remote_address
from flask_login import current_user, login_required
from sqlalchemy.exc import IntegrityError

from report_parser import ReportParseError

from attendance_engine import DayVerdict

from . import db, limiter, planning
from .merge import MergeError, MergeResult, ingest, resolve_proposals
from .models import LectureInstance, PlannedAbsence, Subject
from .services import (
    UNKNOWN_STATUSES,
    apply_prediction_map,
    apply_subject_edits,
    coverage_for,
    dashboard_for,
    predictions_for,
    set_predictions,
    settings_for,
    subject_limit,
    subjects_for,
)

bp = Blueprint("api", __name__, url_prefix="/api")

MAX_PDF_BYTES = 5 * 1024 * 1024

#: Parsing a PDF is the one expensive thing a signed-in user can ask for, so
#: it is the one thing metered per account rather than per address.
UPLOAD_LIMIT = "20 per minute"

#: Ceilings on the lists a request may carry. Each is far above anything the
#: pages send; they exist so a hand-built request can't make one worker do an
#: afternoon's arithmetic.
MAX_BATCH_LECTURES = 50
MAX_SIMULATED_ABSENCES = 500
MAX_TIMETABLE_BLOCKS = 300
MAX_PREDICTION_RESTORE = 5000

#: Dates outside this window are typos, not plans — and the far end of the
#: calendar is where date arithmetic overflows.
MIN_DATE = date(2000, 1, 1)
MAX_FUTURE_DAYS = 731


# ---------------------------------------------------------------------------
# Reading a request. JSON can hold anything, so nothing below is trusted to be
# the type the pages happen to send.
# ---------------------------------------------------------------------------


def _json_object() -> dict:
    """The request body as a dict.

    Anything else — no body, a list, a bare string — reads as empty, so each
    endpoint's own validation answers it with its usual 400.
    """
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _as_date(value) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    if not MIN_DATE <= parsed <= date.today() + timedelta(days=MAX_FUTURE_DAYS):
        return None
    return parsed


def _as_id(value) -> int | None:
    """A row id, from a JSON number or a numeric string."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if 0 < number < 2 ** 63 else None


def _as_text(value, limit: int) -> str | None:
    """Free text trimmed to its column's width; anything that isn't text is
    dropped rather than stored."""
    if not isinstance(value, str):
        return None
    return value.strip()[:limit] or None


def idempotent(view):
    """Run a "make it so" endpoint again when it loses a race with itself.

    Marking a holiday, planning an absence and guessing at a lecture all check
    for an existing row and insert one if there is none. Two copies of the
    same request — a double tap, a retry on a flaky connection, a second tab —
    can both pass the check, and the unique index then refuses the slower one.
    That request asked for a state that now exists, so the right answer is the
    one the faster request got: run it again and let it find the row.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except IntegrityError:
            db.session.rollback()
            planning.forget_derived()
            return view(*args, **kwargs)
    return wrapper


def _user_key() -> str:
    if current_user.is_authenticated:
        return f"user:{current_user.id}"
    return get_remote_address()


@bp.post("/reports")
@login_required
@limiter.limit(UPLOAD_LIMIT, key_func=_user_key)
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
    decisions = _json_object().get("decisions") or {}
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
    return jsonify(_stats_payload())


def _stats_payload() -> dict:
    """Every figure a page might need to repaint itself after a guess.

    One payload rather than three endpoints, because a guess moves all of them
    at once: the subject's own percentages, the untouched worst case beside
    them, and — once planning is on — the budget those numbers feed.
    """
    dash = dashboard_for(current_user)
    coverage = coverage_for(current_user)
    guesses = predictions_for(current_user)
    # The safe reading has to stay reachable: a guess that turns out wrong
    # should be visible as a guess, not just quietly wrong.
    true_worst = dashboard_for(current_user, use_predictions=False) if guesses else None

    # One query for the lot: this runs on every guess, and fifty of them is the
    # normal case rather than the extreme one.
    by_subject: dict[int, int] = {}
    if guesses:
        rows = (
            db.session.query(LectureInstance.subject_id)
            .filter(LectureInstance.id.in_(guesses))
            .all()
        )
        for (subject_id,) in rows:
            by_subject[subject_id] = by_subject.get(subject_id, 0) + 1

    payload = {
        "overall": {
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
        "subjects": [
            {
                "id": s.subject_id,
                "code": s.code,
                "name": s.canonical_name,
                "type": s.lecture_type,
                "limit": s.limit,
                "present": s.counts.present,
                "absent": s.counts.absent,
                "pending": s.counts.unknown,
                "guessed": by_subject.get(s.subject_id, 0),
                "worst_pct": _pct(s.worst_pct),
                "official_pct": _pct(s.official_pct),
                "best_pct": _pct(s.best_pct),
                "can_miss": s.can_miss_effective,
                "recover_needed": s.recover_needed,
                "verdict": s.verdict.value,
                # The status badge is a sentence about all three of these, so
                # the client needs them all to repaint it (templates/_macros.html).
                "pending_dominated": s.pending_dominated,
            }
            for s in dash.subjects
        ],
        "guesses": {
            "count": len(guesses),
            "true_worst": {
                "overall_pct": _pct(true_worst.overall.worst_pct),
                "can_miss": true_worst.overall.can_miss,
            } if true_worst else None,
        },
        "coverage": {
            "gaps": [str(g) for g in coverage.gaps],
            "stale_days": coverage.stale_days,
            "is_stale": coverage.is_stale,
            "pending_count": coverage.pending_count,
            "suggested_export": str(coverage.suggested_export)
            if coverage.suggested_export else None,
        },
    }

    if planning.advanced_ready(current_user)[0]:
        # No `days`: a guess changes the budget, and the strip is a projection
        # of it that the page will ask for separately if it needs one.
        payload["wallet"] = _wallet_payload(with_days=False)
    return payload


@bp.post("/calendar/day")
@login_required
@idempotent
def calendar_day():
    """Tap a day: normal → holiday → normal."""
    payload = _json_object()
    on_date = _as_date(payload.get("date"))
    if on_date is None:
        return jsonify(error="Bad date."), 400

    if on_date < date.today():
        return jsonify(error="Bunkk only plans forwards — past days can't change."), 400

    kind = payload.get("kind")
    if kind not in (None, "holiday"):
        return jsonify(error="Unknown day type."), 400

    planning.set_day(current_user, on_date, kind,
                     name=_as_text(payload.get("name"), 120))
    return jsonify(ok=True, date=on_date.isoformat(), kind=kind)


#: A term's worth of days at once would more likely be a mis-typed year than a
#: holiday, and marking every remaining day off is not a thing anyone means.
MAX_RANGE_DAYS = 92


@bp.post("/calendar/range")
@login_required
@idempotent
def calendar_range():
    """Mark a whole stretch off — a mid-sem break, a festival week."""
    payload = _json_object()
    frm = _as_date(payload.get("from"))
    to = _as_date(payload.get("to"))
    if frm is None or to is None:
        return jsonify(error="Bad date."), 400

    kind = payload.get("kind", "holiday")
    if kind not in (None, "holiday"):
        return jsonify(error="Unknown day type."), 400
    if to < frm:
        return jsonify(error="That range ends before it starts."), 400
    if frm < date.today():
        return jsonify(error="Bunkk only plans forwards — past days can't change."), 400
    if (to - frm).days + 1 > MAX_RANGE_DAYS:
        return jsonify(error="That's longer than a semester — check the dates."), 400

    touched = planning.set_days(current_user, frm, to, kind,
                                name=_as_text(payload.get("name"), 120))
    return jsonify(ok=True, kind=kind, dates=[d.isoformat() for d in touched])


@bp.route("/lectures/<int:lecture_id>/prediction", methods=["PUT", "DELETE"])
@login_required
@idempotent
def lecture_prediction(lecture_id: int):
    """Say how you expect an unmarked lecture to resolve, or take it back."""
    lecture = db.session.get(LectureInstance, lecture_id)
    if lecture is None or lecture.user_id != current_user.id:
        return jsonify(error="Not found."), 404

    if request.method == "DELETE":
        previous = set_predictions(current_user, None, lecture_ids=[lecture_id])
        return jsonify(_prediction_result(None, previous))

    # Guessing at a lecture the college has already marked would be overwriting
    # fact with opinion, which is the one thing this feature must never do.
    if lecture.status not in UNKNOWN_STATUSES:
        return jsonify(error="The college has already marked that one."), 409

    predicted = _json_object().get("predicted")
    if predicted not in ("P", "A"):
        return jsonify(error="Predict either P or A."), 400

    previous = set_predictions(current_user, predicted, lecture_ids=[lecture_id])
    return jsonify(_prediction_result(predicted, previous))


@bp.post("/subjects/<int:subject_id>/predictions")
@login_required
@idempotent
def subject_predictions(subject_id: int):
    """Guess at every unmarked lecture of one subject in one go.

    Fifty pending lectures is a normal state two months into term. One at a
    time, each with a page reload, is not something anyone finishes.
    """
    subject = db.session.get(Subject, subject_id)
    if subject is None or subject.user_id != current_user.id:
        return jsonify(error="Unknown subject."), 404

    predicted = _json_object().get("predicted")
    if predicted not in ("P", "A", None):
        return jsonify(error="Predict either P or A, or null to clear."), 400

    previous = set_predictions(current_user, predicted, subject_id=subject_id)
    return jsonify(_prediction_result(predicted, previous))


@bp.post("/predictions/bulk")
@login_required
@idempotent
def bulk_predictions():
    """Every pending lecture at once, or a named set of them.

    The named form is what Undo posts: the `previous` map from any of these
    endpoints goes straight back in as `lectures`.
    """
    payload = _json_object()

    if "lectures" in payload:
        wanted = payload["lectures"]
        if not isinstance(wanted, dict) or len(wanted) > MAX_PREDICTION_RESTORE:
            return jsonify(error="Malformed restore."), 400
        changes: dict[int, str | None] = {}
        for key, value in wanted.items():
            if not (value is None or value in ("P", "A")):
                return jsonify(error="Predict either P or A, or null to clear."), 400
            lecture_id = _as_id(key)
            if lecture_id is None:
                return jsonify(error="Malformed restore."), 400
            changes[lecture_id] = value
        previous = apply_prediction_map(current_user, changes)
        return jsonify(_prediction_result(None, previous))

    predicted = payload.get("predicted")
    if predicted not in ("P", "A", None):
        return jsonify(error="Predict either P or A, or null to clear."), 400

    previous = set_predictions(current_user, predicted)
    return jsonify(_prediction_result(predicted, previous))


def _prediction_result(predicted: str | None,
                       previous: dict[int, str | None]) -> dict:
    """What every guess endpoint answers with: what changed, and the new totals.

    The memo drop is load-bearing. `predictions_for` and `counts_by_subject` are
    cached per request, so building this after a write without clearing them
    would answer with the numbers from before the guess — and the page would
    patch itself back to exactly what it was already showing.
    """
    planning.forget_derived()
    return {
        "ok": True,
        "changed": len(previous),
        "predicted": predicted,
        "previous": {str(k): v for k, v in previous.items()},
        "stats": _stats_payload(),
    }


@bp.get("/day/<on_date>")
@login_required
def day_sheet(on_date: str):
    """One day's lectures and what is already planned against them.

    Keyed by (subject, start) rather than by time alone: a timetable can run
    two subjects in the same slot, and two lectures of one subject in a day.
    """
    day = _as_date(on_date)
    if day is None:
        return jsonify(error="Bad date."), 400

    holiday = next(
        (h for h in planning.holidays_for(current_user) if h.on_date == day), None
    )
    codes = {s.id: s.code for s in subjects_for(current_user)}
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

    # One wallet for the whole answer: the day's verdict and every lecture's
    # remaining budget come out of the same projection, so they can't disagree.
    ready, _ = planning.advanced_ready(current_user)
    wallet = planning.wallet_for(current_user) if ready else None
    plan = planning.day_plan_on(current_user, day, wallet=wallet) if wallet else None
    budgets = wallet.by_id() if wallet else {}

    occurrences = planning.lectures_on(current_user, day)
    # Two classes timetabled into one slot is a real thing colleges do; the
    # sheet says so rather than showing two rows that look like a mistake.
    starts = [o.slot.start_time for o in occurrences]

    return jsonify(
        date=day.isoformat(),
        label=day.strftime("%A %d %b"),
        is_past=day < date.today(),
        in_semester=bool(end and day <= end),
        # False past the next checkpoint: the absence is real and will be
        # stored, it just doesn't spend from the budget on screen yet.
        in_horizon=bool(win.remaining_to and day <= win.remaining_to),
        horizon_to=win.remaining_to.isoformat() if win.remaining_to else None,
        holiday=({"name": holiday.name} if holiday else None),
        whole_day_absence_id=(whole_day.id if whole_day else None),
        plan=_day_entry(plan),
        lectures=[
            {
                "subject_id": o.slot.subject_id,
                "code": codes.get(o.slot.subject_id, "?"),
                "start": o.slot.start_time.isoformat(),
                "end": o.slot.end_time.isoformat(),
                "absence_id": absence_for(o.slot.subject_id, o.slot.start_time),
                "budget": (budgets[o.slot.subject_id].budget
                           if o.slot.subject_id in budgets else None),
                "verdict": (budgets[o.slot.subject_id].verdict.value
                            if o.slot.subject_id in budgets else None),
                "same_slot": starts.count(o.slot.start_time) > 1,
            }
            for o in occurrences
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
        partial=_partial_payload(plan),
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
    on_date = _as_date(payload.get("date"))
    if on_date is None:
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
        subject_id = _as_id(subject_id)
        subject = db.session.get(Subject, subject_id) if subject_id else None
        if subject is None or subject.user_id != current_user.id:
            return None, (jsonify(error="Unknown subject."), 404)

    try:
        start_time = time.fromisoformat(raw_start) if raw_start else None
    except (TypeError, ValueError):
        return None, (jsonify(error="Bad time."), 400)
    if subject_id is None:
        # A whole day has no start; a time stored on one would make it a row
        # nothing else recognises as the whole day.
        start_time = None

    # An absence against a lecture the timetable doesn't have would count zero
    # anyway; refusing it says so instead of silently storing a no-op. The same
    # is true of a whole day with nothing on it — a Sunday, or a holiday — which
    # used to be accepted and then sit in the committed list meaning nothing.
    lectures = planning.lectures_on(current_user, on_date)
    if subject_id is None:
        if not lectures:
            return None, (jsonify(error="No classes that day."), 422)
    else:
        matches = [o for o in lectures if o.slot.subject_id == subject_id
                   and (start_time is None
                        or planning.same_minute(o.slot.start_time, start_time))]
        if not matches:
            return None, (jsonify(error="Your timetable has no such class that day."), 422)

    # The unique indexes are the guarantee (models.PlannedAbsence); this check
    # is what makes asking twice answer with the row instead of an error.
    existing = (
        db.session.query(PlannedAbsence)
        .filter_by(user_id=current_user.id, on_date=on_date,
                   subject_id=subject_id, start_time=start_time)
        .first()
    )
    if existing is None:
        existing = PlannedAbsence(
            user_id=current_user.id, on_date=on_date, subject_id=subject_id,
            start_time=start_time, note=_as_text(note, 200),
        )
        db.session.add(existing)
    return existing, None


@bp.post("/absences")
@login_required
@idempotent
def add_absence():
    """Commit to missing a future date (whole day, or one subject on it)."""
    payload = _json_object()
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
    return jsonify(dict(_wallet_payload(focus=on_date), absence_id=row.id))


@bp.post("/absences/batch")
@login_required
@idempotent
def add_absences():
    """Commit to missing several lectures of one day at once.

    "Leave after 12:00" is one decision, not four. Posting it as four requests
    raced them against each other and recomputed the whole wallet four times
    for an answer that only had to be worked out once.
    """
    payload = _json_object()
    on_date, error = _day_guard(payload)
    if error:
        return error

    wanted = payload.get("lectures")
    if not isinstance(wanted, list) or not wanted:
        return jsonify(error="Nothing to plan."), 400
    if len(wanted) > MAX_BATCH_LECTURES:
        return jsonify(error="That's more lectures than a day holds."), 400

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
    return jsonify(dict(_wallet_payload(focus=on_date), absence_ids=ids))


@bp.delete("/absences/<int:absence_id>")
@login_required
def remove_absence(absence_id: int):
    row = db.session.get(PlannedAbsence, absence_id)
    if row is None or row.user_id != current_user.id:
        return jsonify(error="Not found."), 404
    # Read before the delete: the row is the only thing that knows which day the
    # caller is looking at, and it is about to stop existing.
    on_date = row.on_date
    db.session.delete(row)
    db.session.commit()
    return jsonify(_wallet_payload(focus=on_date))


@bp.post("/simulate")
@login_required
def simulate_plan():
    """Hypothetical absences — nothing is stored."""
    payload = _json_object()
    wanted = payload.get("absences") or []
    if not isinstance(wanted, list) or len(wanted) > MAX_SIMULATED_ABSENCES:
        return jsonify(error="Malformed absence in the plan."), 400

    extras = []
    for item in wanted:
        if not isinstance(item, dict):
            return jsonify(error="Malformed absence in the plan."), 400
        on_date = _as_date(item.get("date"))
        subject_id = item.get("subject_id")
        if subject_id is not None:
            subject_id = _as_id(subject_id)
        start = item.get("start")
        try:
            start_time = time.fromisoformat(start) if start else None
        except (TypeError, ValueError):
            start_time = False
        if on_date is None or start_time is False or (
                item.get("subject_id") is not None and subject_id is None):
            return jsonify(error="Malformed absence in the plan."), 400
        extras.append((on_date, subject_id, start_time))

    # "light" is the pre-commit check: it asks whether a plan would break
    # anything, and never draws the strip it would have to project to answer
    # anything else.
    return jsonify(_wallet_payload(extras, with_days=not payload.get("light")))


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
    if len(payload["blocks"]) > MAX_TIMETABLE_BLOCKS:
        return jsonify(error="That's more blocks than a week holds."), 400

    # Narrowed to what will actually be stored before anything is judged on it:
    # `save_timetable` blanks the current version before writing, so a grid
    # whose classes all named someone else's subject used to pass this guard,
    # wipe the timetable, and still be answered with `ok: true, classes: 1`.
    entries = planning.storable_entries(current_user,
                                        planning.entries_from(payload["blocks"]))
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

    siblings = subjects_for(current_user)
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


def _day_entry(plan) -> dict | None:
    """One day, as every surface needs it.

    The strip, the day sheet and Today's hero all describe the same day, so they
    read the same dict rather than three views assembling their own.
    """
    if plan is None:
        return None
    return {
        "date": plan.on_date.isoformat(),
        "label": plan.on_date.strftime("%A %d %b"),
        "verdict": plan.verdict.value,
        "reason": plan.reason,
        "planned_count": plan.planned_count,
        "over_budget": plan.over_budget,
        "whole_day": plan.whole_day,
        "leave_after": plan.leave_after.isoformat() if plan.leave_after else None,
        "arrive_at": plan.arrive_at.isoformat() if plan.arrive_at else None,
        "lectures": [
            {"code": l.code, "start": l.start_time.isoformat()}
            for l in plan.lectures
        ],
    }


def _wallet_payload(extras=None, *, focus: date | None = None,
                    with_days: bool = True) -> dict:
    """The recomputed wallet, and optionally the strip and one focused day.

    `with_days=False` skips the whole-horizon projection: the pre-commit safety
    check asks only "would this break anything?", and paying for a strip nobody
    is going to look at made a confirmation dialog feel like a page load.
    """
    result = planning.simulate_for(current_user, extras or [])
    wallet = result.wallet
    payload = {
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
    }

    if with_days:
        payload["days"] = [
            _day_entry(d)
            for d in planning.horizon_strip(current_user, wallet=wallet)
        ]
    if focus is not None:
        # The page that raised this request is showing one day; sending its
        # fresh plan back is what lets the hero repaint instead of reload.
        payload["day"] = _day_entry(
            planning.day_plan_on(current_user, focus, wallet=wallet)
        )
    return payload


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
