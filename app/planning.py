"""Advanced-mode services: timetable, calendar, wallet, day strip.

Where `services.py` feeds the basic-mode engine, this feeds the planning engine.
The interesting decision lives in `_windows`: which stretch of the calendar is
"already happened but unreported" (unknowns you can no longer influence) and
which is "still to come" (attendable, and therefore spendable).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta

from attendance_engine import (
    BreakSpan,
    CalendarRules,
    LadderRung,
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
    skip_ladder,
)

from . import db
from .models import (
    Checkpoint,
    Holiday,
    LectureInstance,
    PlannedAbsence,
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
    #: Where projections stop: the next checkpoint, or the semester end.
    #: None only when the semester end isn't set yet.
    remaining_to: date | None
    #: Set when `remaining_to` is a checkpoint rather than the end of term, so
    #: pages can name the horizon they just quoted a budget for.
    checkpoint: date | None = None
    checkpoint_label: str | None = None
    semester_end: date | None = None

    @property
    def horizon_is_checkpoint(self) -> bool:
        return self.checkpoint is not None

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
    """The engine's view of the grid: classes only.

    Breaks are filtered out here rather than downstream, so there is no code
    path by which a free period reaches the attendance maths and inflates or
    deflates the number of lectures left.
    """
    return [
        Slot(row.subject_id, row.weekday, row.start_time, row.end_time)
        for row in timetable_entries(user)
        if row.kind == "class" and row.subject_id is not None
    ]


def break_spans(user) -> dict[int, list[BreakSpan]]:
    """The grid's free periods, by weekday.

    Kept apart from `active_slots` on purpose: a break must never be counted as
    a lecture. It only informs *where* to cut a day — leaving before an hour of
    nothing is worth more than leaving after it — never how much of the budget
    that cut spends.
    """
    spans: dict[int, list[BreakSpan]] = {}
    for row in timetable_entries(user):
        if row.kind != "break":
            continue
        spans.setdefault(row.weekday, []).append(
            BreakSpan(row.start_time, row.end_time, row.label)
        )
    return spans


def _breaks_over(user, days: list[date]) -> dict[date, list[BreakSpan]]:
    """The weekly break pattern projected onto actual dates."""
    weekly = break_spans(user)
    if not weekly:
        return {}
    rules = calendar_rules(user)
    out: dict[date, list[BreakSpan]] = {}
    for day in days:
        weekday = rules.weekday_for(day)
        if weekday is None:
            continue                    # a holiday has no day to cut short
        found = weekly.get(weekday)
        if found:
            out[day] = found
    return out


def timetable_entries(user) -> list[TimetableSlot]:
    """Everything in the current grid, breaks included — for rendering it."""
    version = active_version(user)
    if version is None:
        return []
    return (
        db.session.query(TimetableSlot)
        .filter_by(version_id=version.id)
        .order_by(TimetableSlot.weekday, TimetableSlot.start_time)
        .all()
    )


@dataclass(frozen=True)
class Entry:
    """One block a user drew on the grid: a class, or a break.

    Lives here rather than in `attendance_engine` because breaks are a display
    concern — the engine's `Slot` stays strictly about lectures.
    """

    weekday: int
    start_time: time
    end_time: time
    kind: str = "class"                 # class|break
    subject_id: int | None = None
    label: str | None = None

    @property
    def is_valid(self) -> bool:
        if not 0 <= self.weekday <= 6 or self.start_time >= self.end_time:
            return False
        if self.kind == "class":
            return self.subject_id is not None
        return self.kind == "break"


def entries_from(rows) -> list[Entry]:
    """Build the grid from a sequence of {kind, weekday, start, end, ...} dicts.

    Shared by the form POST on /timetable and the JSON PUT on /api/timetable so
    the two can never disagree about what a block is. Anything malformed is
    dropped rather than failing the whole save — a half-saved timetable is worse
    than a missing block the user can see is missing.
    """
    entries: list[Entry] = []
    for row in rows:
        try:
            kind = row.get("kind")
            subject_id = row.get("subject_id")
            entry = Entry(
                weekday=int(row["weekday"]),
                start_time=_as_time(row["start"]),
                end_time=_as_time(row["end"]),
                kind=kind if kind in ("class", "break") else "class",
                subject_id=(int(subject_id) if subject_id not in (None, "") else None),
                label=(row.get("label") or None),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if entry.is_valid:
            entries.append(entry)
    return entries


def _as_time(value) -> time:
    if isinstance(value, time):
        return value
    # An <input type="time"> gives "HH:MM"; the ledger gives "HH:MM:SS".
    return time.fromisoformat(str(value))


#: A gap has to be long enough to be a break rather than a walk between rooms,
#: and short enough that it isn't simply the end of the day.
MIN_GAP_MINUTES = 15
MAX_GAP_MINUTES = 180


def fill_gaps(entries: list[Entry]) -> list[Entry]:
    """Draw the free periods a timetable implies as actual break blocks.

    If Monday runs 11:00–12:00 and then 13:00–14:00, the hour between them is a
    break whether or not anyone typed one — and saying so is what makes "leave
    before lunch" worth more than "leave at 12:00 and sit around".

    Called where a grid is first drafted, never on every save: the client fills
    gaps live as you edit (js/timetable.js), so a break you delete stays
    deleted instead of reappearing the next time you press Done. Keep the two
    implementations in step.
    """
    filled = list(entries)
    by_day: dict[int, list[Entry]] = {}
    for entry in entries:
        by_day.setdefault(entry.weekday, []).append(entry)

    for weekday, blocks in by_day.items():
        blocks = sorted(blocks, key=lambda e: e.start_time)
        for prev, nxt in zip(blocks, blocks[1:]):
            if nxt.start_time <= prev.end_time:
                continue                       # touching, or overlapping
            minutes = _minutes_between(prev.end_time, nxt.start_time)
            if not MIN_GAP_MINUTES <= minutes <= MAX_GAP_MINUTES:
                continue
            filled.append(Entry(
                weekday=weekday,
                start_time=prev.end_time,
                end_time=nxt.start_time,
                kind="break",
                label=_gap_label(prev.end_time, nxt.start_time, minutes),
            ))
    return sorted(filled, key=lambda e: (e.weekday, e.start_time))


def _minutes_between(start: time, end: time) -> int:
    return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)


def _gap_label(start: time, end: time, minutes: int) -> str | None:
    """A long-enough gap starting around midday is lunch; the rest is a break.

    Returning None is not a failure — the grid renders an unlabelled break as
    "Break", which is the honest thing to call a free period at 10am.
    """
    midday = time(11, 30) <= start <= time(13, 30)
    return "Lunch" if minutes >= 45 and midday else None


def save_timetable(user, entries: list[Entry | Slot], *, source: str = "inferred",
                   valid_from: date | None = None,
                   replace: bool = True) -> TimetableVersion:
    """Store a grid as an effective-dated version.

    A mid-semester reshuffle becomes a new row so past projections stay
    explicable. But the editor now saves on every Done rather than on one
    button at the bottom, and a version per keystroke-session would bury that
    history in noise — so `replace` rewrites today's own manual version in
    place and only starts a new one when the day, or the source, changes.

    Accepts engine `Slot`s as well as `Entry`s, because inference still speaks
    in Slots and there is no reason to make callers convert.
    """
    semester = active_semester(user)
    if semester is None:
        raise ValueError("No semester yet — upload a report first.")

    effective = valid_from or date.today()
    version = None
    if replace:
        current = active_version(user)
        if (current is not None and current.source == source
                and current.valid_from == effective):
            db.session.query(TimetableSlot).filter_by(version_id=current.id).delete()
            version = current

    if version is None:
        version = TimetableVersion(
            semester_id=semester.id,
            valid_from=effective,
            source=source,
        )
        db.session.add(version)
    db.session.flush()

    owned = {s.id for s in db.session.query(Subject).filter_by(user_id=user.id).all()}
    for entry in entries:
        if isinstance(entry, Slot):
            entry = Entry(entry.weekday, entry.start_time, entry.end_time,
                          kind="class", subject_id=entry.subject_id)
        if not entry.is_valid:
            continue
        # Never trust ids from a form: an unowned subject is dropped, not saved.
        if entry.kind == "class" and entry.subject_id not in owned:
            continue
        db.session.add(
            TimetableSlot(
                version_id=version.id,
                weekday=entry.weekday,
                start_time=entry.start_time,
                end_time=entry.end_time,
                kind=entry.kind,
                subject_id=entry.subject_id if entry.kind == "class" else None,
                label=(entry.label or None) if entry.kind == "break" else None,
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
    return CalendarRules(
        holidays=frozenset(row.on_date for row in holidays_for(user))
    )


def set_day(user, on_date: date, kind: str | None, *, name: str | None = None) -> None:
    """Mark a date as a holiday, or clear it back to a normal day."""
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
    existing.name = name
    db.session.commit()


def semester_end(user) -> date | None:
    semester = active_semester(user)
    return semester.end_date if semester else None


def checkpoints_for(user) -> list[Checkpoint]:
    """Every checkpoint in the active semester, in date order."""
    semester = active_semester(user)
    if semester is None:
        return []
    return (
        db.session.query(Checkpoint)
        .filter_by(semester_id=semester.id)
        .order_by(Checkpoint.on_date)
        .all()
    )


def next_checkpoint(user, on_or_after: date | None = None) -> Checkpoint | None:
    """The next audit you have to clear, if there is one before the end."""
    semester = active_semester(user)
    if semester is None:
        return None
    on_or_after = on_or_after or date.today()
    row = (
        db.session.query(Checkpoint)
        .filter(Checkpoint.semester_id == semester.id,
                Checkpoint.on_date >= on_or_after)
        .order_by(Checkpoint.on_date)
        .first()
    )
    # The end date can be moved earlier after a checkpoint was added, so clamp
    # on read as well as validating on write.
    if row is None or (semester.end_date and row.on_date > semester.end_date):
        return None
    return row


def horizon_end(user, on_or_after: date | None = None) -> date | None:
    """The date every projection runs to.

    Returns None exactly when `semester_end` does. That equivalence is what
    keeps checkpoints a small change: every downstream `if remaining_to` guard
    already means "is the semester set up yet", and still does.
    """
    end = semester_end(user)
    if end is None:
        return None
    row = next_checkpoint(user, on_or_after)
    return min(row.on_date, end) if row else end


def set_semester_end(user, end: date) -> None:
    semester = active_semester(user)
    if semester is not None:
        semester.end_date = end
        db.session.commit()


def add_checkpoint(user, on_date: date, label: str | None = None,
                   today: date | None = None) -> str | None:
    """Add an audit date. Returns an error message, or None on success."""
    semester = active_semester(user)
    if semester is None:
        return "Upload a report first — Bunkr needs to know your semester."
    if on_date <= (today or date.today()):
        return "A checkpoint has to be in the future."
    if semester.end_date and on_date > semester.end_date:
        return "That's after your semester ends."
    existing = (
        db.session.query(Checkpoint)
        .filter_by(semester_id=semester.id, on_date=on_date)
        .one_or_none()
    )
    if existing is not None:
        return "You already have a checkpoint on that date."

    db.session.add(Checkpoint(semester_id=semester.id, on_date=on_date,
                              label=(label or None)))
    db.session.commit()
    return None


def remove_checkpoint(user, checkpoint_id: int) -> bool:
    semester = active_semester(user)
    if semester is None:
        return False
    row = (
        db.session.query(Checkpoint)
        .filter_by(id=checkpoint_id, semester_id=semester.id)
        .one_or_none()
    )
    if row is None:
        return False
    db.session.delete(row)
    db.session.commit()
    return True


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
        unreported_from = unreported_to = None
        remaining_from = today
    else:
        unreported_from = latest + timedelta(days=1)
        unreported_to = today - timedelta(days=1)
        remaining_from = max(today, latest + timedelta(days=1))

    end = semester_end(user)
    checkpoint = next_checkpoint(user, remaining_from)
    remaining_to = horizon_end(user, remaining_from)

    return Windows(
        unreported_from=unreported_from,
        unreported_to=unreported_to,
        remaining_from=remaining_from,
        remaining_to=remaining_to,
        checkpoint=(checkpoint.on_date if checkpoint and checkpoint.on_date != end else None),
        checkpoint_label=(checkpoint.label if checkpoint and checkpoint.on_date != end else None),
        semester_end=end,
    )


def same_minute(a, b) -> bool:
    """Compare two times to the minute.

    The portal prints lecture times as "08:00:01 AM" and the parser keeps that
    faithfully, but an <input type="time"> can only ever give back "08:00". So
    a slot edited by hand would stop matching an absence booked against the
    imported one, and the absence would silently stop counting. Nobody runs two
    lectures a second apart, so the seconds carry no information worth keeping.
    """
    if a is None or b is None:
        return a is b
    return (a.hour, a.minute) == (b.hour, b.minute)


def _absence_hits(on_date: date, subject_id: int | None, start_time,
                  slots: list[Slot], rules: CalendarRules) -> list[Occurrence]:
    """Which real lectures a planned absence actually covers.

    Every tier resolves against the timetable rather than being taken at its
    word, so an absence filed against a day the subject doesn't run counts
    zero — instead of counting one and relying on a downstream clamp to hide it.
    """
    occurrences = expand(slots, on_date, on_date, rules)
    if subject_id is None:
        return occurrences
    hits = [o for o in occurrences if o.slot.subject_id == subject_id]
    if start_time is None:
        return hits
    return [o for o in hits if same_minute(o.slot.start_time, start_time)]


def _planned_absence_counts(user, slots: list[Slot], rules: CalendarRules,
                            frm: date, to: date | None) -> dict[int, int]:
    """Planned absences per subject, resolved through the timetable."""
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
        for occ in _absence_hits(row.on_date, row.subject_id, row.start_time,
                                 slots, rules):
            tally[occ.slot.subject_id] = tally.get(occ.slot.subject_id, 0) + 1
    return tally


def wallet_rows(user, today: date | None = None,
                extra_absences: list[tuple] | None = None) -> list[dict]:
    """The wallet's inputs, one row per subject.

    Split out from `wallet_for` so the skip ladder can build ten hypothetical
    wallets from a single pass over the database instead of ten.
    `extra_absences` are hypothetical and stored nowhere.
    """
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

    for extra in (extra_absences or []):
        # Tuples may be (date, subject) or (date, subject, start_time).
        on_date, subject_id = extra[0], extra[1]
        start_time = extra[2] if len(extra) > 2 else None
        if not (win.remaining_from <= on_date <= (win.remaining_to or on_date)):
            continue
        for occ in _absence_hits(on_date, subject_id, start_time, slots, rules):
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
    return rows


def wallet_for(user, today: date | None = None,
               extra_absences: list[tuple] | None = None) -> Wallet:
    return build_wallet(
        wallet_rows(user, today, extra_absences),
        overall_limit=settings_for(user).overall_limit,
    )


def skip_ladder_for(user, subject_id: int, max_n: int | None = None,
                    today: date | None = None) -> list[LadderRung]:
    """What skipping 1, 2, 3... more lectures of one subject would cost."""
    return skip_ladder(
        wallet_rows(user, today),
        settings_for(user).overall_limit,
        subject_id,
        max_n,
    )


def upcoming_occurrences(user, subject_id: int | None = None, *,
                         limit: int | None = None,
                         today: date | None = None,
                         exclude_planned: bool = True) -> list[Occurrence]:
    """The next lectures the timetable says will happen, inside the horizon.

    Goes through `expand`, so holidays are honoured for free. Used to turn a
    ladder rung ("skip 3 of these") into the three real dates that get stored.
    """
    win = windows(user, today)
    if win.remaining_to is None:
        return []

    slots = active_slots(user)
    rules = calendar_rules(user)
    occurrences = expand(slots, win.remaining_from, win.remaining_to, rules)
    if subject_id is not None:
        occurrences = [o for o in occurrences if o.slot.subject_id == subject_id]

    if exclude_planned:
        # Re-opening the ladder after committing must not offer the same
        # lectures again, or the second commit double-counts them.
        planned = {
            (r.on_date, r.subject_id, r.start_time)
            for r in db.session.query(PlannedAbsence).filter_by(user_id=user.id).all()
        }

        def is_free(o: Occurrence) -> bool:
            sid, start = o.slot.subject_id, o.slot.start_time
            return not (
                (o.on_date, None, None) in planned
                or (o.on_date, sid, None) in planned
                or (o.on_date, sid, start) in planned
            )

        occurrences = [o for o in occurrences if is_free(o)]

    occurrences.sort(key=lambda o: (o.on_date, o.slot.start_time))
    return occurrences[:limit] if limit else occurrences


def lectures_on(user, on_date: date) -> list[Occurrence]:
    """One day's lectures — the calendar day sheet, and write-time validation."""
    return expand(active_slots(user), on_date, on_date, calendar_rules(user))


def breaks_on(user, on_date: date) -> list[BreakSpan]:
    """One day's free periods, in clock order — for drawing the day as it runs."""
    return sorted(
        _breaks_over(user, [on_date]).get(on_date, []),
        key=lambda b: b.start_time,
    )


def planned_count_by_date(user, rows) -> dict[date, int]:
    """How many real lectures each date's planned absences actually cover.

    A day is rarely all-or-nothing now that single classes can be planned, so
    the strip shows a count rather than a marker. Resolved through the timetable
    for the same reason the budget is: a whole-day row on a day with three
    lectures is worth three, and one filed against a class that doesn't run is
    worth nothing.
    """
    return {day: len(lectures)
            for day, lectures in _planned_lecture_hits(user, rows).items()}


def _planned_lecture_hits(user, rows) -> dict[date, set]:
    """Which real lectures the given absence rows cover, as
    ``date -> {(subject_id, start_time)}``.

    Keyed by the lecture itself: a whole-day row and a single-class row on the
    same day overlap, and counting both would claim more missed lectures than
    the day even holds.
    """
    if not rows:
        return {}
    slots, rules = active_slots(user), calendar_rules(user)
    hit_lectures: dict[date, set] = {}
    for row in rows:
        for occurrence in _absence_hits(
            row.on_date, row.subject_id, row.start_time, slots, rules
        ):
            hit_lectures.setdefault(row.on_date, set()).add(
                (occurrence.slot.subject_id, occurrence.slot.start_time)
            )
    return hit_lectures


def absences_on(user, on_date: date) -> tuple[PlannedAbsence | None, dict]:
    """What is already planned for one date: the whole-day row, and a map of
    ``(subject_id, "HH:MM") -> absence id`` for the per-lecture ones.

    Today and the calendar day sheet both need to ask "is this lecture already
    planned?", and they must answer it identically — a subject-wide row covers
    every occurrence of that subject on the day, while a row carrying a time
    covers only its own. Keyed to the minute because a hand-edited slot loses
    the portal's seconds (see `same_minute`).
    """
    rows = (
        db.session.query(PlannedAbsence)
        .filter_by(user_id=user.id, on_date=on_date)
        .all()
    )
    whole_day = next(
        (r for r in rows if r.subject_id is None and r.start_time is None), None
    )
    per_lecture = {}
    for occurrence in lectures_on(user, on_date):
        slot = occurrence.slot
        for row in rows:
            if row.subject_id != slot.subject_id:
                continue
            if row.start_time is None or same_minute(row.start_time, slot.start_time):
                per_lecture[(slot.subject_id, slot.start_time.strftime("%H:%M"))] = row.id
                break
    return whole_day, per_lecture


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


def _to_planned(occurrences: list[Occurrence], codes: dict[int, str],
                committed: dict[date, set] | None = None) -> dict[date, list[PlannedLecture]]:
    """The engine's view of a stretch of days, each lecture flagged with whether
    an absence is already committed against it."""
    committed = committed or {}
    days: dict[date, list[PlannedLecture]] = {}
    for occ in occurrences:
        key = (occ.slot.subject_id, occ.slot.start_time)
        days.setdefault(occ.on_date, []).append(
            PlannedLecture(
                subject_id=occ.slot.subject_id,
                code=codes.get(occ.slot.subject_id, "?"),
                on_date=occ.on_date,
                start_time=occ.slot.start_time,
                end_time=occ.slot.end_time,
                planned=key in committed.get(occ.on_date, ()),
            )
        )
    return days


def _committed_between(user, frm: date, to: date) -> dict[date, set]:
    """Lectures already committed as missed, between two dates inclusive."""
    rows = (
        db.session.query(PlannedAbsence)
        .filter(PlannedAbsence.user_id == user.id)
        .filter(PlannedAbsence.on_date >= frm, PlannedAbsence.on_date <= to)
        .all()
    )
    return _planned_lecture_hits(user, rows)


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
    return day_plans(
        _to_planned(occurrences, codes, _committed_between(user, start, end)),
        wallet, horizon, _breaks_over(user, horizon),
    )


def horizon_strip(user, today: date | None = None,
                  wallet: Wallet | None = None) -> list[DayPlan]:
    """Every day from here to the horizon, not a fixed fortnight.

    `STRIP_DAYS` used to cap this at two weeks, which meant a trip you already
    knew about in November had nowhere to go. The horizon is the honest limit:
    past it, the days belong to the next checkpoint's budget, not this one.
    """
    win = windows(user, today)
    days = STRIP_DAYS
    if win.remaining_to:
        days = max(1, (win.remaining_to - win.remaining_from).days + 1)
    return day_strip(user, today=today, days=days, wallet=wallet)


def day_plan_on(user, on_date: date, wallet: Wallet | None = None) -> DayPlan | None:
    """One specific day's plan, for any date in the semester.

    `day_strip` only looks forward from the projection window; Today's arrows
    walk the whole current week, including days already behind you. Those days
    still have a timetable, so they still have lectures worth listing — they
    just no longer have a decision attached.
    """
    slots = active_slots(user)
    if not slots:
        return None

    wallet = wallet or wallet_for(user)
    codes = {
        s.id: s.code
        for s in db.session.query(Subject).filter_by(user_id=user.id).all()
    }
    occurrences = expand(slots, on_date, on_date, calendar_rules(user))
    plans = day_plans(
        _to_planned(occurrences, codes, _committed_between(user, on_date, on_date)),
        wallet, [on_date], _breaks_over(user, [on_date]),
    )
    return plans[0] if plans else None


# ---------------------------------------------------------------------------
# Setup gate
# ---------------------------------------------------------------------------


def advanced_ready(user) -> tuple[bool, str | None]:
    """Is advanced mode usable, and if not, what's the single next step?"""
    if active_semester(user) is None:
        return False, "upload"
    if not has_timetable(user):
        return False, "timetable"
    if semester_end(user) is None:
        return False, "calendar"
    return True, None
