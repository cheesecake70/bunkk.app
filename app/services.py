"""The bridge between the database and the pure engine.

Everything user-scoped is queried through here so row-level isolation is
enforced in one place (§6.4 privacy). The engine never sees a model object;
this module hands it plain `Counts`.
"""
from __future__ import annotations

from datetime import date

from attendance_engine import Counts, CoverageReport, Dashboard, DateRange
from attendance_engine import analyse, build_dashboard

from . import db
from .cache import per_request
from .models import (
    LectureChange,
    LectureInstance,
    LecturePrediction,
    ReportSnapshot,
    Semester,
    Settings,
    Subject,
    User,
)

#: How report statuses fold into the engine's three buckets (§4).
#: AG (attendance granted) is presumed present; L (late) is an unknown —
#: worst-case absent, best-case present — so it joins the NU pile.
PRESENT_STATUSES = ("P", "AG")
ABSENT_STATUSES = ("A",)
UNKNOWN_STATUSES = ("NU", "L")


@per_request
def settings_for(user: User) -> Settings:
    settings = db.session.get(Settings, user.id)
    if settings is None:
        settings = Settings(user_id=user.id)
        db.session.add(settings)
        db.session.commit()
    return settings


@per_request
def active_semester(user: User) -> Semester | None:
    return (
        db.session.query(Semester)
        .filter_by(user_id=user.id, is_active=True)
        .order_by(Semester.id.desc())
        .first()
    )


def subjects_for(user: User, *, active_only: bool = False) -> list[Subject]:
    """The subjects of the active semester, in code order.

    Every page that lists subjects goes through here, so a subject from last
    term can never turn up in this term's table — and so the query that used
    to be copied into six views lives in one.
    """
    semester = active_semester(user)
    if semester is None:
        return []
    query = db.session.query(Subject).filter_by(user_id=user.id,
                                                semester_id=semester.id)
    if active_only:
        query = query.filter_by(active=True)
    return query.order_by(Subject.code).all()


def _ledger_query(user: User):
    """Lectures of the active semester, for everything that counts them."""
    semester = active_semester(user)
    query = (
        db.session.query(LectureInstance)
        .join(Subject, Subject.id == LectureInstance.subject_id)
        .filter(LectureInstance.user_id == user.id)
        .filter(LectureInstance.is_vanished.is_(False))
    )
    # An impossible id rather than no filter: with no semester there is no
    # ledger to show, and "no filter" would show every term at once.
    return query.filter(Subject.semester_id == (semester.id if semester else -1))


def subject_limit(subject: Subject, settings: Settings) -> int:
    return subject.custom_limit if subject.custom_limit is not None else settings.subject_limit


def apply_subject_edits(subject: Subject, siblings: list[Subject],
                        values: dict) -> dict[str, str]:
    """Validate and apply one subject's edits, returning field -> message.

    Lives here rather than in the page that grew it so the batch form on
    /subjects and the per-card PUT on /api/subjects/<id> can never drift into
    disagreeing about what a valid subject is. Keys are bare field names
    ("name", "code", "custom_limit"); the form route prefixes them with the row
    id itself.

    `values` is a mapping the caller has already narrowed to this subject. A key
    it omits is left untouched — that is what lets a partial edit stay partial
    instead of reading every absent field as "cleared".
    """
    errors: dict[str, str] = {}

    def text(field: str) -> str:
        # JSON can put anything here; only a string is a name.
        value = values.get(field)
        return value.strip() if isinstance(value, str) else ""

    if "name" in values:
        name = text("name")
        if not name:
            errors["name"] = "A subject needs a name."
        elif len(name) > 200:
            errors["name"] = "That name is too long."
        else:
            subject.canonical_name = name

    if "code" in values:
        code = text("code")
        if not code:
            errors["code"] = "A short code keeps the tables readable."
        elif len(code) > 20:
            errors["code"] = "Keep the short code under 20 characters."
        else:
            # Codes label every table and chip in the app, so two subjects
            # sharing one would make the numbers unreadable rather than merely
            # untidy.
            clash = any(
                other.id != subject.id and (other.code or "").lower() == code.lower()
                for other in siblings
            )
            if clash:
                errors["code"] = "Another subject already uses that short code."
            else:
                subject.code = code

    if "custom_limit" in values:
        raw = values.get("custom_limit")
        # `0` is a limit, not a blank — only None and "" mean "use the default".
        raw = "" if raw is None or isinstance(raw, bool) else str(raw).strip()
        if not raw:
            subject.custom_limit = None        # blank means "use the default"
        else:
            try:
                limit = int(raw)
            except (TypeError, ValueError):
                errors["custom_limit"] = "Must be a whole number."
            else:
                if not 0 <= limit <= 100:
                    errors["custom_limit"] = "Must be between 0 and 100."
                else:
                    subject.custom_limit = limit

    return errors


@per_request
def predictions_for(user: User) -> dict[int, str]:
    """lecture_id -> 'P'|'A', for lectures the college still hasn't marked.

    The join is what retires a guess: once a report flips NU to a real status
    the row stops being returned, so a fresh upload always wins without
    anything needing to be deleted.
    """
    semester = active_semester(user)
    rows = (
        db.session.query(LecturePrediction.lecture_id, LecturePrediction.predicted)
        .join(LectureInstance, LectureInstance.id == LecturePrediction.lecture_id)
        .join(Subject, Subject.id == LectureInstance.subject_id)
        .filter(LecturePrediction.user_id == user.id)
        .filter(Subject.semester_id == (semester.id if semester else -1))
        .filter(LectureInstance.is_vanished.is_(False))
        .filter(LectureInstance.status.in_(UNKNOWN_STATUSES))
        .all()
    )
    return {lecture_id: predicted for lecture_id, predicted in rows}


def set_predictions(user: User, predicted: str | None, *,
                    subject_id: int | None = None,
                    lecture_ids: list[int] | None = None) -> dict[int, str | None]:
    """Guess at every unmarked lecture in scope at once. Returns the old values.

    Fifty pending lectures is a normal state for a student two months into term,
    and answering them one at a time — with a page reload after each — is not a
    feature anyone will finish using. The scope is always "lectures the college
    hasn't marked": a guess can never overwrite something the portal has said,
    whichever way it is asked for.

    The returned {lecture_id: previous} map is what makes Undo possible without
    the client having to remember what it changed.
    """
    if predicted not in ("P", "A", None):
        raise ValueError("Predict either P or A, or None to clear.")

    query = (
        _ledger_query(user)
        .with_entities(LectureInstance.id)
        .filter(LectureInstance.status.in_(UNKNOWN_STATUSES))
    )
    if subject_id is not None:
        query = query.filter(LectureInstance.subject_id == subject_id)
    if lecture_ids is not None:
        query = query.filter(LectureInstance.id.in_(lecture_ids))

    targets = {row.id for row in query.all()}
    if not targets:
        return {}

    existing = {
        row.lecture_id: row
        for row in db.session.query(LecturePrediction)
        .filter(LecturePrediction.user_id == user.id)
        .filter(LecturePrediction.lecture_id.in_(targets))
        .all()
    }

    # Only what actually moves is recorded. Counting every lecture in scope
    # would have "Clear" on a subject with no guesses report fifty cleared, and
    # the caller uses this same count to decide there was nothing to do.
    previous: dict[int, str | None] = {}
    for lecture_id in targets:
        row = existing.get(lecture_id)
        was = row.predicted if row else None
        if was == predicted:
            continue

        previous[lecture_id] = was
        if predicted is None:
            db.session.delete(row)
        elif row is None:
            db.session.add(LecturePrediction(user_id=user.id, lecture_id=lecture_id,
                                             predicted=predicted))
        else:
            row.predicted = predicted

    if previous:
        db.session.commit()
    return previous


def apply_prediction_map(user: User, changes: dict[int, str | None]) -> dict[int, str | None]:
    """Put a set of guesses back exactly as they were — the Undo path.

    Grouped by value so a mixed restore is a handful of scoped queries rather
    than one per lecture.
    """
    restored: dict[int, str | None] = {}
    by_value: dict[str | None, list[int]] = {}
    for lecture_id, value in changes.items():
        by_value.setdefault(value, []).append(int(lecture_id))

    for value, ids in by_value.items():
        restored.update(set_predictions(user, value, lecture_ids=ids))
    return restored


@per_request
def counts_by_subject(user: User, *, use_predictions: bool = True) -> dict[int, Counts]:
    """Fold the ledger into per-subject tallies.

    This is the single place a status becomes a `Counts`, which is why guesses
    are applied here: every number in the app is built from these three buckets,
    so one change propagates everywhere without the engine knowing predictions
    exist at all.

    Pass `use_predictions=False` for the untouched worst case — Overview shows
    it beside the guessed figures so the safe reading is never more than a
    glance away.

    Lectures flagged `is_vanished` are excluded: the portal no longer reports
    them, so counting them would put our totals out of step with the college's
    own. They stay in the ledger and are surfaced for review instead of deleted.
    """
    rows = _ledger_query(user).all()
    guesses = predictions_for(user) if use_predictions else {}

    tally: dict[int, dict[str, int]] = {}
    for row in rows:
        bucket = tally.setdefault(row.subject_id, {"p": 0, "a": 0, "n": 0})
        status = guesses.get(row.id, row.status)
        if status in PRESENT_STATUSES:
            bucket["p"] += 1
        elif status in ABSENT_STATUSES:
            bucket["a"] += 1
        else:
            bucket["n"] += 1
    return {
        sid: Counts(present=b["p"], absent=b["a"], unknown=b["n"])
        for sid, b in tally.items()
    }


def dashboard_for(user: User, *, use_predictions: bool = True) -> Dashboard:
    settings = settings_for(user)
    counts = counts_by_subject(user, use_predictions=use_predictions)
    subjects = subjects_for(user, active_only=True)
    rows = [
        {
            "id": s.id,
            "code": s.code,
            "canonical_name": s.canonical_name,
            "lecture_type": s.lecture_type,
            "limit": subject_limit(s, settings),
            "counts": counts.get(s.id, Counts()),
        }
        for s in subjects
    ]
    return build_dashboard(rows, overall_limit=settings.overall_limit)


def subject_worst_percentages(user: User) -> dict[str, float | None]:
    """Worst-case % per subject code — used for upload before/after diffs."""
    dash = dashboard_for(user)
    return {
        s.code: (float(s.worst_pct) if s.worst_pct is not None else None)
        for s in dash.subjects
    }


def coverage_for(user: User, today: date | None = None) -> CoverageReport:
    """Resolve `today` before the memo, so callers that pass it explicitly and
    callers that don't share one cached answer instead of two."""
    return _coverage_for(user, today or date.today())


@per_request
def _coverage_for(user: User, today: date) -> CoverageReport:
    semester = active_semester(user)
    snapshots = (
        db.session.query(ReportSnapshot)
        .filter_by(user_id=user.id, status="merged",
                   semester_id=(semester.id if semester else -1))
        .all()
    )
    ranges = [DateRange(s.period_start, s.period_end) for s in snapshots]

    pending = (
        _ledger_query(user)
        .filter(LectureInstance.status.in_(UNKNOWN_STATUSES))
        .all()
    )
    settings = settings_for(user)
    return analyse(
        ranges,
        today=today,
        staleness_days=settings.staleness_days,
        pending_count=len(pending),
        oldest_pending=min((p.on_date for p in pending), default=None),
    )


def lectures_for_subject(user: User, subject_id: int) -> list[LectureInstance]:
    return (
        db.session.query(LectureInstance)
        .filter_by(user_id=user.id, subject_id=subject_id)
        .order_by(LectureInstance.on_date.desc(), LectureInstance.start_time.desc())
        .all()
    )


def changes_for_lectures(user: User, lecture_ids: list[int]) -> dict[int, list[LectureChange]]:
    """The status history of these lectures, scoped to their owner.

    `LectureChange` has no `user_id` of its own, so the scope has to come from
    the lecture it belongs to. Every caller happens to pass ids it already
    fetched user-scoped, but this module promises isolation in one place rather
    than in every caller.
    """
    if not lecture_ids:
        return {}
    rows = (
        db.session.query(LectureChange)
        .join(LectureInstance, LectureInstance.id == LectureChange.lecture_id)
        .filter(LectureInstance.user_id == user.id)
        .filter(LectureChange.lecture_id.in_(lecture_ids))
        .order_by(LectureChange.changed_at)
        .all()
    )
    history: dict[int, list[LectureChange]] = {}
    for row in rows:
        history.setdefault(row.lecture_id, []).append(row)
    return history


def vanished_lectures(user: User) -> list[LectureInstance]:
    """This term's lectures the portal stopped reporting."""
    semester = active_semester(user)
    return (
        db.session.query(LectureInstance)
        .join(Subject, Subject.id == LectureInstance.subject_id)
        .filter(LectureInstance.user_id == user.id)
        .filter(LectureInstance.is_vanished.is_(True))
        .filter(Subject.semester_id == (semester.id if semester else -1))
        .order_by(LectureInstance.on_date.desc())
        .all()
    )

