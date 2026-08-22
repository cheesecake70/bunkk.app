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


def settings_for(user: User) -> Settings:
    settings = db.session.get(Settings, user.id)
    if settings is None:
        settings = Settings(user_id=user.id)
        db.session.add(settings)
        db.session.commit()
    return settings


def active_semester(user: User) -> Semester | None:
    return (
        db.session.query(Semester)
        .filter_by(user_id=user.id, is_active=True)
        .order_by(Semester.id.desc())
        .first()
    )


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

    if "name" in values:
        name = (values.get("name") or "").strip()
        if not name:
            errors["name"] = "A subject needs a name."
        elif len(name) > 200:
            errors["name"] = "That name is too long."
        else:
            subject.canonical_name = name

    if "code" in values:
        code = (values.get("code") or "").strip()
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
        raw = str(values.get("custom_limit") or "").strip()
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


def predictions_for(user: User) -> dict[int, str]:
    """lecture_id -> 'P'|'A', for lectures the college still hasn't marked.

    The join is what retires a guess: once a report flips NU to a real status
    the row stops being returned, so a fresh upload always wins without
    anything needing to be deleted.
    """
    rows = (
        db.session.query(LecturePrediction.lecture_id, LecturePrediction.predicted)
        .join(LectureInstance, LectureInstance.id == LecturePrediction.lecture_id)
        .filter(LecturePrediction.user_id == user.id)
        .filter(LectureInstance.is_vanished.is_(False))
        .filter(LectureInstance.status.in_(UNKNOWN_STATUSES))
        .all()
    )
    return {lecture_id: predicted for lecture_id, predicted in rows}


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
    rows = (
        db.session.query(LectureInstance)
        .filter_by(user_id=user.id, is_vanished=False)
        .all()
    )
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
    subjects = (
        db.session.query(Subject)
        .filter_by(user_id=user.id, active=True)
        .order_by(Subject.code)
        .all()
    )
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
    snapshots = (
        db.session.query(ReportSnapshot)
        .filter_by(user_id=user.id, status="merged")
        .all()
    )
    ranges = [DateRange(s.period_start, s.period_end) for s in snapshots]

    pending = (
        db.session.query(LectureInstance)
        .filter_by(user_id=user.id, is_vanished=False)
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


def changes_for_lectures(lecture_ids: list[int]) -> dict[int, list[LectureChange]]:
    if not lecture_ids:
        return {}
    rows = (
        db.session.query(LectureChange)
        .filter(LectureChange.lecture_id.in_(lecture_ids))
        .order_by(LectureChange.changed_at)
        .all()
    )
    history: dict[int, list[LectureChange]] = {}
    for row in rows:
        history.setdefault(row.lecture_id, []).append(row)
    return history


def vanished_lectures(user: User) -> list[LectureInstance]:
    return (
        db.session.query(LectureInstance)
        .filter_by(user_id=user.id, is_vanished=True)
        .order_by(LectureInstance.on_date.desc())
        .all()
    )


def snapshots_for(user: User) -> list[ReportSnapshot]:
    return (
        db.session.query(ReportSnapshot)
        .filter_by(user_id=user.id)
        .order_by(ReportSnapshot.uploaded_at.desc())
        .all()
    )
