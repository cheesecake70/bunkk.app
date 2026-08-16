"""Advanced-mode services: timetable, calendar, wallet, day strip.

Where `services.py` feeds the basic-mode engine, this feeds the planning engine.
The interesting decision lives in `_windows`: which stretch of the calendar is
"already happened but unreported" (unknowns you can no longer influence) and
which is "still to come" (attendable, and therefore spendable).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from attendance_engine import (
    CalendarRules,
    DayPlan,
    LectureRow,
    Occurrence,
    PlannedLecture,
    Slot,
    SlotCandidate,
    Wallet,
    build_wallet,
    count_by_subject,
    day_plans,
    expand,
    infer_slots,
    simulate,
)

from . import db
from .models import (
    Holiday,
    LectureInstance,
    PlannedAbsence,
    Semester,
    Subject,
    TimetableSlot,
    TimetableVersion,
)
from .services import active_semester, counts_by_subject, coverage_for, settings_for, subject_limit

#: How many days the GO/SKIP strip looks ahead.
STRIP_DAYS = 14


@dataclass(frozen=True)
class Windows:
    """The two forward-looking stretches of the semester."""

    unreported_from: date | None
    unreported_to: date | None
    remaining_from: date
    remaining_to: date | None       # None when the semester end isn't set yet

    @property
    def has_unreported(self) -> bool:
        return (
            self.unreported_from is not None
            and self.unreported_to is not None
            and self.unreported_from <= self.unreported_to
        )


# ---------------------------------------------------------------------------
# Timetable
# ---------------------------------------------------------------------------


def inferred_candidates(user) -> list[SlotCandidate]:
    rows = [
        LectureRow(l.subject_id, l.on_date, l.start_time, l.end_time)
        for l in db.session.query(LectureInstance)
        .filter_by(user_id=user.id, is_vanished=False)
        .all()
    ]
    return infer_slots(rows)


def active_version(user) -> TimetableVersion | None:
    semester = active_semester(user)
    if semester is None:
        return None
    return (
        db.session.query(TimetableVersion)
        .filter_by(semester_id=semester.id)
        .order_by(TimetableVersion.valid_from.desc(), TimetableVersion.id.desc())
        .first()
    )


def active_slots(user) -> list[Slot]:
    version = active_version(user)
    if version is None:
        return []
    return [
        Slot(row.subject_id, row.weekday, row.start_time, row.end_time)
        for row in db.session.query(TimetableSlot).filter_by(version_id=version.id).all()
    ]


def save_timetable(user, slots: list[Slot], *, source: str = "inferred",
                   valid_from: date | None = None) -> TimetableVersion:
    """Store a confirmed grid as a new effective-dated version.

    Versions are never edited in place: a mid-semester reshuffle becomes a new
    row so past projections stay explicable.
    """
    semester = active_semester(user)
    if semester is None:
        raise ValueError("No semester yet — upload a report first.")

    version = TimetableVersion(
        semester_id=semester.id,
        valid_from=valid_from or date.today(),
        source=source,
    )
    db.session.add(version)
    db.session.flush()

    owned = {s.id for s in db.session.query(Subject).filter_by(user_id=user.id).all()}
    for slot in slots:
        if slot.subject_id not in owned:          # never trust ids from a form
            continue
        db.session.add(
            TimetableSlot(
                version_id=version.id,
                weekday=slot.weekday,
                start_time=slot.start_time,
                end_time=slot.end_time,
                subject_id=slot.subject_id,
            )
        )
    db.session.commit()
    return version


def has_timetable(user) -> bool:
    return bool(active_slots(user))


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def holidays_for(user) -> list[Holiday]:
    semester = active_semester(user)
    if semester is None:
        return []
    return (
        db.session.query(Holiday)
        .filter_by(semester_id=semester.id)
        .order_by(Holiday.on_date)
        .all()
    )


def calendar_rules(user) -> CalendarRules:
    holidays, swaps = set(), {}
    for row in holidays_for(user):
        if row.kind == "swap" and row.swap_weekday is not None:
            swaps[row.on_date] = row.swap_weekday
        else:
            holidays.add(row.on_date)
    return CalendarRules(holidays=frozenset(holidays), swaps=swaps)


def set_day(user, on_date: date, kind: str | None, *, name: str | None = None,
            swap_weekday: int | None = None) -> None:
    """Mark a date as holiday/swap, or clear it back to a normal day."""
    semester = active_semester(user)
    if semester is None:
        return
    existing = (
        db.session.query(Holiday)
        .filter_by(semester_id=semester.id, on_date=on_date)
        .one_or_none()
    )
    if kind is None:
        if existing is not None:
            db.session.delete(existing)
        db.session.commit()
        return

    if existing is None:
        existing = Holiday(semester_id=semester.id, on_date=on_date)
        db.session.add(existing)
    existing.kind = kind
    existing.name = name
    existing.swap_weekday = swap_weekday if kind == "swap" else None
    db.session.commit()


def semester_end(user) -> date | None:
    semester = active_semester(user)
    return semester.end_date if semester else None


def set_semester_end(user, end: date) -> None:
    semester = active_semester(user)
    if semester is not None:
        semester.end_date = end
        db.session.commit()


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def windows(user, today: date | None = None) -> Windows:
    """Split the calendar around what the reports actually cover.

    Today counts as *remaining* — you can still choose to go — unless a report
    already covers today, in which case those lectures are in the ledger and
    projecting them again would count them twice.
    """
    today = today or date.today()
    latest = coverage_for(user, today=today).latest_covered

    if latest is None:
        return Windows(None, None, today, semester_end(user))

    return Windows(
        unreported_from=latest + timedelta(days=1),
        unreported_to=today - timedelta(days=1),
        remaining_from=max(today, latest + timedelta(days=1)),
        remaining_to=semester_end(user),
    )


def _planned_absence_counts(user, slots: list[Slot], rules: CalendarRules,
                            frm: date, to: date | None) -> dict[int, int]:
    """Planned absences per subject, expanding whole-day plans via the timetable."""
    if to is None or frm > to:
        return {}
    rows = (
        db.session.query(PlannedAbsence)
        .filter(PlannedAbsence.user_id == user.id)
        .filter(PlannedAbsence.on_date >= frm, PlannedAbsence.on_date <= to)
        .all()
    )
    tally: dict[int, int] = {}
    for row in rows:
        if row.subject_id is not None:
            tally[row.subject_id] = tally.get(row.subject_id, 0) + 1
            continue
        for occ in expand(slots, row.on_date, row.on_date, rules):
            tally[occ.slot.subject_id] = tally.get(occ.slot.subject_id, 0) + 1
    return tally


def wallet_for(user, today: date | None = None,
               extra_absences: list[tuple[date, int | None]] | None = None) -> Wallet:
    """The bunk wallet. `extra_absences` are hypothetical, for the simulator."""
    today = today or date.today()
    slots = active_slots(user)
    rules = calendar_rules(user)
    win = windows(user, today)
    settings = settings_for(user)

    unreported = (
        count_by_subject(expand(slots, win.unreported_from, win.unreported_to, rules))
        if win.has_unreported else {}
    )
    remaining = (
        count_by_subject(expand(slots, win.remaining_from, win.remaining_to, rules))
        if win.remaining_to else {}
    )
    planned = _planned_absence_counts(user, slots, rules,
                                      win.remaining_from, win.remaining_to)

    for on_date, subject_id in (extra_absences or []):
        if not (win.remaining_from <= on_date <= (win.remaining_to or on_date)):
            continue
        if subject_id is not None:
            planned[subject_id] = planned.get(subject_id, 0) + 1
        else:
            for occ in expand(slots, on_date, on_date, rules):
                planned[occ.slot.subject_id] = planned.get(occ.slot.subject_id, 0) + 1

    counts = counts_by_subject(user)
    subjects = (
        db.session.query(Subject)
        .filter_by(user_id=user.id, active=True)
        .order_by(Subject.code)
        .all()
    )
    from attendance_engine import Counts

    rows = [
        {
            "id": s.id,
            "code": s.code,
            "canonical_name": s.canonical_name,
            "lecture_type": s.lecture_type,
            "limit": subject_limit(s, settings),
            "counts": counts.get(s.id, Counts()),
            "unreported": unreported.get(s.id, 0),
            "remaining": remaining.get(s.id, 0),
            # Never plan to miss more than actually remain.
            "planned_absences": min(planned.get(s.id, 0), remaining.get(s.id, 0)),
        }
        for s in subjects
    ]
    return build_wallet(rows, overall_limit=settings.overall_limit)


def simulate_for(user, extra_absences, today: date | None = None):
    """Same inputs as `wallet_for`, but reports what the plan would break."""
    wallet = wallet_for(user, today=today, extra_absences=extra_absences)
    rows = [
        {
            "id": s.subject_id, "code": s.code, "canonical_name": s.canonical_name,
            "lecture_type": s.lecture_type, "limit": s.limit, "counts": s.counts,
            "unreported": s.unreported, "remaining": s.remaining,
            "planned_absences": s.planned_absences,
        }
        for s in wallet.subjects
    ]
    return simulate(rows, overall_limit=settings_for(user).overall_limit)


# ---------------------------------------------------------------------------
# Day strip
# ---------------------------------------------------------------------------


def _to_planned(occurrences: list[Occurrence], codes: dict[int, str]) -> dict[date, list[PlannedLecture]]:
    days: dict[date, list[PlannedLecture]] = {}
    for occ in occurrences:
        days.setdefault(occ.on_date, []).append(
            PlannedLecture(
                subject_id=occ.slot.subject_id,
                code=codes.get(occ.slot.subject_id, "?"),
                on_date=occ.on_date,
                start_time=occ.slot.start_time,
                end_time=occ.slot.end_time,
            )
        )
    return days


def day_strip(user, today: date | None = None, days: int = STRIP_DAYS,
              wallet: Wallet | None = None) -> list[DayPlan]:
    today = today or date.today()
    wallet = wallet or wallet_for(user, today)
    slots = active_slots(user)
    if not slots:
        return []

    win = windows(user, today)
    start = win.remaining_from
    end = start + timedelta(days=days - 1)
    if win.remaining_to and win.remaining_to < end:
        end = win.remaining_to
    if start > end:
        return []

    codes = {
        s.id: s.code
        for s in db.session.query(Subject).filter_by(user_id=user.id).all()
    }
    occurrences = expand(slots, start, end, calendar_rules(user))
    horizon = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return day_plans(_to_planned(occurrences, codes), wallet, horizon)


# ---------------------------------------------------------------------------
# The nudge that carries its own incentive (Phase 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnlockEstimate:
    """What a fresh upload would buy you.

    Every unresolved lecture — pending in the ledger, or held since the last
    report — is counted as missed. If they all turn out to be attended, this is
    how much budget was being withheld. It is deliberately the *best* case: the
    honest way to say it is "up to N", and it is never used to authorise a bunk.
    """

    current_budget: int
    optimistic_budget: int
    unresolved: int

    @property
    def gain(self) -> int:
        return max(0, self.optimistic_budget - self.current_budget)

    @property
    def worth_nudging(self) -> bool:
        return self.unresolved > 0 and self.gain > 0


def unlock_estimate(user, today: date | None = None) -> UnlockEstimate:
    from attendance_engine import Counts

    wallet = wallet_for(user, today)
    settings = settings_for(user)

    optimistic_rows = [
        {
            "id": s.subject_id, "code": s.code, "canonical_name": s.canonical_name,
            "lecture_type": s.lecture_type, "limit": s.limit,
            # Everything unresolved lands as "present" in the best case.
            "counts": Counts(
                present=s.counts.present + s.counts.unknown + s.unreported,
                absent=s.counts.absent,
                unknown=0,
            ),
            "unreported": 0,
            "remaining": s.remaining,
            "planned_absences": s.planned_absences,
        }
        for s in wallet.subjects
    ]
    optimistic = build_wallet(optimistic_rows, overall_limit=settings.overall_limit)

    return UnlockEstimate(
        current_budget=wallet.overall_budget,
        optimistic_budget=optimistic.overall_budget,
        unresolved=wallet.overall_counts.unknown + wallet.overall_unreported,
    )


@dataclass(frozen=True)
class Brief:
    """One glanceable sentence — the notification, and the PWA's answer."""

    title: str
    body: str
    url: str = "/"
    tag: str = "bunkmate-morning"


def morning_brief(user, today: date | None = None) -> Brief | None:
    """Tomorrow's verdict in a sentence, or None when there's nothing to say."""
    today = today or date.today()
    tomorrow = today + timedelta(days=1)
    unlock = unlock_estimate(user, today)
    nudge = (
        f" Upload a fresh report to unlock up to {unlock.gain} more."
        if unlock.worth_nudging else ""
    )

    ready, _ = advanced_ready(user)
    if not ready:
        # Basic mode still has something useful to say every morning.
        coverage = coverage_for(user, today=today)
        if coverage.pending_count:
            return Brief(
                title=f"{coverage.pending_count} lectures still unmarked",
                body=(f"Your real budget is probably bigger than it looks."
                      f"{nudge or ' Upload a fresh report to find out.'}"),
                url="/upload",
            )
        if coverage.is_stale:
            return Brief(
                title="Your attendance is going stale",
                body=f"No report since {coverage.latest_covered:%d %b}. "
                     f"Export {coverage.suggested_export} to catch up.",
                url="/upload",
            )
        return None

    strip = day_strip(user, today=today, days=2)
    plan = next((d for d in strip if d.on_date == tomorrow), None)
    if plan is None:
        return None

    titles = {
        "skip": "Tomorrow: skippable",
        "partial": "Tomorrow: half day",
        "go": "Tomorrow: you need to go",
        "off": "Tomorrow: no classes",
    }
    return Brief(
        title=titles.get(plan.verdict.value, "Tomorrow"),
        body=plan.reason + nudge,
    )


def advanced_ready(user) -> tuple[bool, str | None]:
    """Is advanced mode usable, and if not, what's the single next step?"""
    if active_semester(user) is None:
        return False, "upload"
    if not has_timetable(user):
        return False, "timetable"
    if semester_end(user) is None:
        return False, "calendar"
    return True, None
