"""Timetable inference and expansion — implementation-plan §3.3.

The detailed report implicitly contains the timetable: a lecture that recurs at
the same (subject, weekday, start time) across several weeks *is* a weekly slot.
So the app proposes a grid and the student taps to confirm, instead of filling
one in from scratch.

Two halves, both pure:

  infer_slots  — ledger rows          -> candidate weekly slots + confidence
  expand       — confirmed slots      -> the actual dates they fall on,
                                         honouring holidays and swap-days
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, time, timedelta

#: A pattern seen in fewer distinct weeks than this is offered, but not
#: pre-selected — one-off extra classes shouldn't silently become weekly.
CONFIDENT_WEEKS = 2


@dataclass(frozen=True)
class LectureRow:
    """Minimal ledger row the inference needs (keeps models out of the engine)."""

    subject_id: int
    on_date: date
    start_time: time
    end_time: time


@dataclass(frozen=True)
class Slot:
    subject_id: int
    weekday: int            # 0 = Monday
    start_time: time
    end_time: time


@dataclass(frozen=True)
class SlotCandidate:
    slot: Slot
    occurrences: int
    weeks_seen: int
    first_seen: date
    last_seen: date

    @property
    def is_confident(self) -> bool:
        return self.weeks_seen >= CONFIDENT_WEEKS


@dataclass(frozen=True)
class CalendarRules:
    """What the semester calendar says about specific dates."""

    holidays: frozenset[date] = frozenset()
    #: date -> the weekday whose timetable runs that day ("Friday runs Tuesday's")
    swaps: dict[date, int] | None = None

    def weekday_for(self, day: date) -> int | None:
        """The timetable weekday to run on `day`, or None if nothing runs."""
        if day in self.holidays:
            return None
        if self.swaps and day in self.swaps:
            return self.swaps[day]
        return day.weekday()


@dataclass(frozen=True)
class Occurrence:
    on_date: date
    slot: Slot


def infer_slots(rows: list[LectureRow]) -> list[SlotCandidate]:
    """Group the ledger into recurring weekly slots.

    Identity is (subject, weekday, start time) — never (weekday, start time),
    because two subjects genuinely share a slot in the real data (Thu 13:00
    holds both DBMS Theory and DS Theory).
    """
    groups: dict[tuple[int, int, time], list[LectureRow]] = defaultdict(list)
    for row in rows:
        groups[(row.subject_id, row.on_date.weekday(), row.start_time)].append(row)

    candidates: list[SlotCandidate] = []
    for (subject_id, weekday, start_time), members in groups.items():
        dates = [m.on_date for m in members]
        weeks = {(d.isocalendar()[0], d.isocalendar()[1]) for d in dates}
        # Lectures occasionally run long or short; the usual end time wins.
        end_time = Counter(m.end_time for m in members).most_common(1)[0][0]
        candidates.append(
            SlotCandidate(
                slot=Slot(subject_id, weekday, start_time, end_time),
                occurrences=len(members),
                weeks_seen=len(weeks),
                first_seen=min(dates),
                last_seen=max(dates),
            )
        )

    candidates.sort(key=lambda c: (c.slot.weekday, c.slot.start_time, c.slot.subject_id))
    return candidates


def expand(
    slots: list[Slot],
    start: date,
    end: date,
    rules: CalendarRules | None = None,
) -> list[Occurrence]:
    """Every lecture the timetable says will happen in [start, end] inclusive."""
    if start > end or not slots:
        return []

    rules = rules or CalendarRules()
    by_weekday: dict[int, list[Slot]] = defaultdict(list)
    for slot in slots:
        by_weekday[slot.weekday].append(slot)

    out: list[Occurrence] = []
    day = start
    while day <= end:
        weekday = rules.weekday_for(day)
        if weekday is not None:
            for slot in by_weekday.get(weekday, ()):
                out.append(Occurrence(on_date=day, slot=slot))
        day += timedelta(days=1)

    out.sort(key=lambda o: (o.on_date, o.slot.start_time, o.slot.subject_id))
    return out


def count_by_subject(occurrences: list[Occurrence]) -> dict[int, int]:
    tally: dict[int, int] = defaultdict(int)
    for occ in occurrences:
        tally[occ.slot.subject_id] += 1
    return dict(tally)
