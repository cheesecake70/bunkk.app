"""Advanced mode: projection, bunk wallet, day verdicts — plan §4.

Once the timetable and semester calendar are confirmed, the number of lectures
left is *known*, not estimated, so the budget becomes a spendable wallet:

    R_s  remaining lectures of subject s (tomorrow → semester end)
    G_s  lectures that already happened but no report covers yet
    T_s  = P_s + A_s + N_s + G_s + R_s          (every lecture of the semester)
    b_s  = the most you can still miss and stay at or above the limit

`G_s` matters more than it looks. Lectures held since the last upload cannot be
attended any more, and worst case they were missed — so they belong in the
denominator as unknowns. Leaving them out would quietly overstate the budget,
which is the one failure this app is not allowed to have.

Derivation of the budget, worst case (pending and unreported both count absent,
every remaining lecture attended except the b you spend):

    100·(P_s + R_s − planned_s − b) ≥ L_s · T_s
    b ≤ (100·(P_s + R_s − planned_s) − L_s·T_s) / 100

so b_s = floor(that), clamped into [0, R_s − planned_s] — you cannot skip a
lecture you have already committed to missing, nor more than actually remain.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from datetime import date, time
from fractions import Fraction

from .budget import WARN_MARGIN, ratio
from .types import Counts, Verdict


class DayVerdict(str, enum.Enum):
    SKIP = "skip"        # the whole day fits the budget
    PARTIAL = "partial"  # some of it does
    GO = "go"            # nothing does
    OFF = "off"          # no lectures scheduled
    #: The decision has been made: what is left of the day is either already
    #: committed or unskippable, so there is nothing to advise. Separate from
    #: SKIP because a day you have written off is not the same claim as "this
    #: is safe to skip" — the commitment may well have broken a limit, and
    #: saying SKIP there told the student their own over-spend was fine.
    PLANNED = "planned"


@dataclass(frozen=True)
class BreakSpan:
    """A free period between two lectures.

    Carries no attendance weight — it never reaches the budget maths. It exists
    so that "when could I leave?" is answered in hours off, not lectures
    skipped: leaving at noon when the next class is at one buys the noon hour
    too, and a search that counts only lectures cannot see that.
    """

    start_time: time
    end_time: time
    label: str | None = None


@dataclass(frozen=True)
class PlannedLecture:
    """One projected future lecture."""

    subject_id: int
    code: str
    on_date: date
    start_time: time
    end_time: time
    #: True when an absence is already committed against this lecture. Its cost
    #: has already been taken out of the wallet, so the day verdict must not
    #: charge for it a second time.
    planned: bool = False


@dataclass(frozen=True)
class SubjectPlan:
    subject_id: int
    code: str
    canonical_name: str
    lecture_type: str
    limit: int
    counts: Counts          # what the ledger knows: P / A / N
    unreported: int         # G_s — happened, no report covers it yet
    remaining: int          # R_s
    planned_absences: int
    budget: int             # b_s — bunks left
    verdict: Verdict

    @property
    def projected_total(self) -> int:
        return self.counts.total + self.unreported + self.remaining

    @property
    def projected_worst_pct(self):
        """Where you land if you attend every remaining lecture you haven't
        already written off — the honest ceiling for planning."""
        attended = self.counts.present + self.remaining - self.planned_absences
        return ratio(attended, self.projected_total)


@dataclass(frozen=True)
class Wallet:
    subjects: list[SubjectPlan]
    overall_limit: int
    overall_counts: Counts
    overall_unreported: int
    overall_remaining: int
    overall_planned: int
    overall_budget: int
    overall_verdict: Verdict

    def by_id(self) -> dict[int, SubjectPlan]:
        return {s.subject_id: s for s in self.subjects}

    @property
    def overall_projected_total(self) -> int:
        return (self.overall_counts.total + self.overall_unreported
                + self.overall_remaining)

    @property
    def overall_projected_worst_pct(self) -> Fraction | None:
        """Mirrors SubjectPlan.projected_worst_pct, for the whole ledger."""
        attended = (self.overall_counts.present + self.overall_remaining
                    - self.overall_planned)
        return ratio(attended, self.overall_projected_total)


@dataclass(frozen=True)
class DayPlan:
    on_date: date
    lectures: list[PlannedLecture]
    verdict: DayVerdict
    reason: str
    #: Latest lecture you could leave after and still be within budget.
    leave_after: time | None = None
    #: Earliest lecture you could arrive for and still be within budget.
    arrive_at: time | None = None
    skippable_codes: list[str] = field(default_factory=list)
    #: Wall-clock minutes the cut frees up, breaks included. This is what
    #: ranks the options: two cuts that skip the same lectures are not worth
    #: the same if one of them also gets you out before an hour of nothing.
    freed_minutes: int = 0
    #: Name of a break inside the freed window, when there is one to name.
    covers_break: str | None = None
    #: The lectures the winning cut actually skips, in clock order.
    skippable: list[PlannedLecture] = field(default_factory=list)
    #: How many of the day's lectures already have an absence against them.
    planned_count: int = 0
    #: True when something already committed on this day has put its subject —
    #: or the overall rule — below the limit. The commitment stands; the day
    #: just stops pretending it was free.
    over_budget: bool = False
    #: True when every lecture on the day is committed.
    whole_day: bool = False


# ---------------------------------------------------------------------------
# The wallet
# ---------------------------------------------------------------------------


def _budget(present: int, remaining: int, planned: int, total: int, limit: int) -> int:
    spendable = remaining - planned
    if spendable <= 0:
        return 0
    allowed = (100 * (present + spendable) - limit * total) // 100
    return max(0, min(spendable, allowed))


def build_wallet(rows: list[dict], overall_limit: int) -> Wallet:
    """`rows` are plain dicts, one per subject:
    {id, code, canonical_name, lecture_type, limit, counts, unreported,
     remaining, planned_absences}
    """
    subjects: list[SubjectPlan] = []
    tot = Counts()
    tot_unreported = tot_remaining = tot_planned = 0

    for row in rows:
        counts: Counts = row["counts"]
        unreported, remaining = row["unreported"], row["remaining"]
        planned = row["planned_absences"]
        total = counts.total + unreported + remaining
        budget = _budget(counts.present, remaining, planned, total, row["limit"])

        subjects.append(
            SubjectPlan(
                subject_id=row["id"],
                code=row["code"],
                canonical_name=row["canonical_name"],
                lecture_type=row["lecture_type"],
                limit=row["limit"],
                counts=counts,
                unreported=unreported,
                remaining=remaining,
                planned_absences=planned,
                budget=budget,
                verdict=_verdict(budget, counts.present, remaining, planned,
                                 total, row["limit"]),
            )
        )
        tot = tot + counts
        tot_unreported += unreported
        tot_remaining += remaining
        tot_planned += planned

    grand_total = tot.total + tot_unreported + tot_remaining
    overall_budget = _budget(tot.present, tot_remaining, tot_planned,
                             grand_total, overall_limit)

    # A subject can never be safer than the overall rule allows, and the verdict
    # has to follow the capped budget — otherwise a subject with private slack
    # would read SAFE while its real budget is zero.
    subjects = [
        replace(
            s,
            budget=min(s.budget, overall_budget),
            verdict=_verdict(
                min(s.budget, overall_budget), s.counts.present, s.remaining,
                s.planned_absences, s.projected_total, s.limit,
            ),
        )
        for s in subjects
    ]
    subjects.sort(key=lambda s: (s.budget, s.code))

    return Wallet(
        subjects=subjects,
        overall_limit=overall_limit,
        overall_counts=tot,
        overall_unreported=tot_unreported,
        overall_remaining=tot_remaining,
        overall_planned=tot_planned,
        overall_budget=overall_budget,
        overall_verdict=_verdict(overall_budget, tot.present, tot_remaining,
                                 tot_planned, grand_total, overall_limit),
    )


def _verdict(budget: int, present: int, remaining: int, planned: int,
             total: int, limit: int) -> Verdict:
    if total <= 0:
        return Verdict.UNKNOWN
    # Can the limit still be reached at all, even attending everything left?
    best_possible = 100 * (present + remaining - planned)
    if best_possible < limit * total:
        return Verdict.DANGER
    if budget < WARN_MARGIN:
        return Verdict.WARN
    return Verdict.SAFE


# ---------------------------------------------------------------------------
# Day verdicts
# ---------------------------------------------------------------------------


def day_plans(
    days: dict[date, list[PlannedLecture]],
    wallet: Wallet,
    horizon: list[date],
    breaks: dict[date, list[BreakSpan]] | None = None,
) -> list[DayPlan]:
    """Verdict per upcoming day.

    Each day is judged *on its own*: "if the only thing I skip is this day, am I
    still safe?" Budgets are shared across days, so skipping two green days in a
    row is not automatically safe — the simulator is how you commit to more than
    one, and it re-derives the whole wallet.
    """
    budgets = {s.subject_id: s.budget for s in wallet.subjects}
    breaks = breaks or {}

    # Which subjects the plan has already put under water. Derived from the same
    # verdict `simulate()` reads for its `breaks` list, so a day that says "over
    # budget" and the banner that names the subject can never disagree.
    broken = {s.subject_id for s in wallet.subjects if s.verdict is Verdict.DANGER}
    overall_broken = wallet.overall_verdict is Verdict.DANGER

    return [
        _day_plan(day, days.get(day, []), budgets, wallet.overall_budget,
                  breaks.get(day, []), broken, overall_broken)
        for day in horizon
    ]


def _day_plan(day: date, lectures: list[PlannedLecture],
              budgets: dict[int, int], overall_budget: int,
              day_breaks: list[BreakSpan] | None = None,
              broken: set[int] | None = None,
              overall_broken: bool = False) -> DayPlan:
    if not lectures:
        return DayPlan(on_date=day, lectures=[], verdict=DayVerdict.OFF,
                       reason="No classes scheduled.")

    ordered = sorted(lectures, key=lambda l: l.start_time)
    day_breaks = day_breaks or []
    broken = broken or set()

    # Lectures you have already committed to missing are spent: the wallet
    # deducted them the moment you committed. Only what is still undecided is
    # worth asking about — judging the committed ones again charges the budget
    # twice, which is why a day written off in full used to come back as "part
    # skip", advice about a decision already made.
    committed = [l for l in ordered if l.planned]
    pending = [l for l in ordered if not l.planned]

    over = bool(committed) and (
        overall_broken or any(l.subject_id in broken for l in committed)
    )
    spent = [l.code for l in committed if l.subject_id in broken]
    overspend = _overspend_sentence(spent, overall_broken) if over else ""

    if not pending:
        return DayPlan(
            on_date=day, lectures=ordered, verdict=DayVerdict.PLANNED,
            reason="Skipping the whole day — " + _cost_sentence(ordered) + "."
                   + overspend,
            skippable_codes=sorted({l.code for l in ordered}),
            skippable=ordered,
            freed_minutes=_span(_day_start(ordered, day_breaks),
                                _day_end(ordered, day_breaks)),
            planned_count=len(committed), over_budget=over, whole_day=True,
        )

    if _fits(pending, budgets, overall_budget):
        return DayPlan(
            on_date=day, lectures=ordered, verdict=DayVerdict.SKIP,
            reason=_cost_sentence(pending) + " — all within budget." + overspend,
            skippable_codes=sorted({l.code for l in pending}),
            skippable=pending,
            freed_minutes=_span(_day_start(ordered, day_breaks),
                                _day_end(ordered, day_breaks)),
            planned_count=len(committed), over_budget=over,
        )

    # Every cut that fits, scored by the wall-clock it frees rather than the
    # lectures it skips. That is what makes leaving before a break beat leaving
    # after one: the break falls inside the freed window and counts.
    starts_at = _day_start(ordered, day_breaks)
    ends_at = _day_end(ordered, day_breaks)
    options: list[_Cut] = []

    # Cuts walk the undecided lectures: a class you already wrote off is not one
    # you stay for, nor one you come in at.
    for cut in range(len(pending)):                     # skip the tail
        skipped = pending[cut:]
        if not _fits(skipped, budgets, overall_budget):
            continue
        boundary = pending[cut - 1].end_time if cut > 0 else starts_at
        options.append(_Cut(
            leave_after=(pending[cut - 1].end_time if cut > 0 else None),
            arrive_at=None,
            skipped=skipped,
            freed=_span(boundary, ends_at),
        ))

    for cut in range(len(pending), 0, -1):              # skip the head
        skipped = pending[:cut]
        if not _fits(skipped, budgets, overall_budget):
            continue
        boundary = pending[cut].start_time if cut < len(pending) else ends_at
        options.append(_Cut(
            leave_after=None,
            arrive_at=(pending[cut].start_time if cut < len(pending) else None),
            skipped=skipped,
            freed=_span(starts_at, boundary),
        ))

    if not options:
        # Nothing left to decide. With commitments already on the day that is a
        # settled plan, not a warning: telling someone who has taken the app's
        # own "leave after 09:00" advice that the day is now a MUST GO reads as
        # the advice having been withdrawn.
        if committed:
            return DayPlan(
                on_date=day, lectures=ordered, verdict=DayVerdict.PLANNED,
                reason=_planned_sentence(committed, pending) + overspend,
                leave_after=_leave_boundary(committed, pending),
                arrive_at=_arrive_boundary(committed, pending),
                skippable_codes=sorted({l.code for l in committed}),
                skippable=committed,
                freed_minutes=_span(_day_start(committed, []),
                                    _day_end(committed, [])),
                planned_count=len(committed), over_budget=over,
            )

        blockers = sorted({l.code for l in pending if budgets.get(l.subject_id, 0) <= 0})
        reason = (
            "No room left in " + ", ".join(blockers) + "."
            if blockers else "Skipping any of it would break a limit."
        )
        return DayPlan(on_date=day, lectures=ordered, verdict=DayVerdict.GO,
                       reason=reason)

    # Most time off first; between equal windows, the one that costs fewer
    # attendance marks; and leaving early over arriving late, which is the
    # easier of the two to actually do.
    best = max(options, key=lambda c: (c.freed, -len(c.skipped),
                                       c.leave_after is not None))

    covered = _break_in(day_breaks, best, starts_at, ends_at)
    reason = _partial_sentence(best, covered)

    return DayPlan(
        on_date=day, lectures=ordered, verdict=DayVerdict.PARTIAL,
        reason=reason + overspend,
        leave_after=best.leave_after, arrive_at=best.arrive_at,
        skippable_codes=sorted({l.code for l in best.skipped}),
        skippable=best.skipped,
        freed_minutes=best.freed,
        covers_break=covered,
        planned_count=len(committed), over_budget=over,
    )


@dataclass(frozen=True)
class _Cut:
    """One way of doing part of the day."""

    leave_after: time | None
    arrive_at: time | None
    skipped: list[PlannedLecture]
    freed: int


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _span(start: time, end: time) -> int:
    return max(0, _minutes(end) - _minutes(start))


def _day_start(lectures: list[PlannedLecture], breaks: list[BreakSpan]) -> time:
    return min([l.start_time for l in lectures] + [b.start_time for b in breaks])


def _day_end(lectures: list[PlannedLecture], breaks: list[BreakSpan]) -> time:
    return max([l.end_time for l in lectures] + [b.end_time for b in breaks])


def _break_in(breaks: list[BreakSpan], cut: _Cut, starts_at: time,
              ends_at: time) -> str | None:
    """The break the freed window swallows, if it swallows one worth naming."""
    if cut.leave_after is not None:
        window = (cut.leave_after, ends_at)
    elif cut.arrive_at is not None:
        window = (starts_at, cut.arrive_at)
    else:
        window = (starts_at, ends_at)

    inside = [
        b for b in breaks
        if b.start_time >= window[0] and b.end_time <= window[1]
    ]
    if not inside:
        return None
    longest = max(inside, key=lambda b: _span(b.start_time, b.end_time))
    return longest.label or "the break"


def _hours(minutes: int) -> str:
    if minutes >= 60 and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours}h"
    if minutes >= 60:
        return f"{minutes // 60}h{minutes % 60:02d}"
    return f"{minutes}m"


def _partial_sentence(cut: _Cut, covered: str | None) -> str:
    cost = _cost_sentence(cut.skipped)
    if cut.leave_after is not None:
        head = f"Leave after {cut.leave_after:%H:%M} — skips {cost}"
    elif cut.arrive_at is not None:
        head = f"Arrive by {cut.arrive_at:%H:%M} — skips {cost}"
    else:
        head = f"Part of the day is skippable — {cost}"

    if covered:
        head += f" and {covered}"
    return f"{head}. {_hours(cut.freed)} free."


def _leave_boundary(committed: list[PlannedLecture],
                    pending: list[PlannedLecture]) -> time | None:
    """When the committed lectures are the tail of the day, the time you leave."""
    if not pending or min(l.start_time for l in committed) < max(
        l.end_time for l in pending
    ):
        return None
    return max(l.end_time for l in pending)


def _arrive_boundary(committed: list[PlannedLecture],
                     pending: list[PlannedLecture]) -> time | None:
    """When the committed lectures are the head of the day, the time you arrive."""
    if not pending or max(l.end_time for l in committed) > min(
        l.start_time for l in pending
    ):
        return None
    return min(l.start_time for l in pending)


def _planned_sentence(committed: list[PlannedLecture],
                      pending: list[PlannedLecture]) -> str:
    """What a settled day says: the shape of the plan, then what it leaves."""
    rest = "The rest is a must-attend."
    leave = _leave_boundary(committed, pending)
    if leave is not None:
        return (f"Leaving after {leave:%H:%M} is planned — skips "
                f"{_cost_sentence(committed)}. {rest}")

    arrive = _arrive_boundary(committed, pending)
    if arrive is not None:
        return (f"Arriving by {arrive:%H:%M} is planned — skips "
                f"{_cost_sentence(committed)}. {rest}")

    return f"{_cost_sentence(committed)} planned. {rest}"


def _overspend_sentence(codes: list[str], overall_broken: bool) -> str:
    """Named after what it is: the day's commitments have cost more than it had.

    Kept as a suffix on whatever the day was going to say, because the plan is
    still the plan — the student is being told the price, not asked to redo it.
    """
    named = sorted(set(codes))
    if named:
        return " Over budget: " + ", ".join(named) + "."
    if overall_broken:
        return " Over budget overall."
    return ""


def _fits(lectures: list[PlannedLecture], budgets: dict[int, int],
          overall_budget: int) -> bool:
    """Could this exact set of lectures be missed without breaking anything?"""
    if not lectures:
        return False
    if len(lectures) > overall_budget:
        return False
    per_subject: dict[int, int] = {}
    for lec in lectures:
        per_subject[lec.subject_id] = per_subject.get(lec.subject_id, 0) + 1
    return all(cost <= budgets.get(sid, 0) for sid, cost in per_subject.items())


def _cost_sentence(lectures: list[PlannedLecture]) -> str:
    counts: dict[str, int] = {}
    for lec in lectures:
        counts[lec.code] = counts.get(lec.code, 0) + 1
    parts = [f"{n}× {code}" if n > 1 else code for code, n in sorted(counts.items())]
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SimulationResult:
    wallet: Wallet
    breaks: list[str]           # subject codes pushed below their limit
    overall_breaks: bool

    @property
    def is_safe(self) -> bool:
        return not self.breaks and not self.overall_breaks


def simulate(rows: list[dict], overall_limit: int) -> SimulationResult:
    """Rebuild the wallet with planned absences applied and report what breaks."""
    wallet = build_wallet(rows, overall_limit)
    breaks = [
        s.code for s in wallet.subjects
        if 100 * (s.counts.present + s.remaining - s.planned_absences)
        < s.limit * s.projected_total
    ]
    total_present = wallet.overall_counts.present
    grand_total = (wallet.overall_counts.total + wallet.overall_unreported
                   + wallet.overall_remaining)
    overall_breaks = (
        100 * (total_present + wallet.overall_remaining - wallet.overall_planned)
        < overall_limit * grand_total
    )
    return SimulationResult(wallet=wallet, breaks=breaks, overall_breaks=overall_breaks)


# ---------------------------------------------------------------------------
# The skip ladder
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LadderRung:
    """What skipping `n` more lectures of one subject costs."""

    n: int
    subject_pct: Fraction | None
    subject_verdict: Verdict
    subject_budget_left: int
    overall_pct: Fraction | None
    overall_verdict: Verdict
    #: Subjects this rung breaks that were NOT already broken before it. A
    #: student who is under water everywhere would otherwise see the same ten
    #: codes against every rung, which says nothing about the choice in front
    #: of them.
    breaks: tuple[str, ...]
    #: True when this rung breaks nothing that wasn't already broken.
    is_safe: bool


def skip_ladder(rows: list[dict], overall_limit: int, subject_id: int,
                max_n: int | None = None) -> list[LadderRung]:
    """For n = 1..K, where skipping n more lectures of `subject_id` lands you.

    Counted, not dated. The wallet has no date dimension — `planned_absences`
    is an integer — so "which three lectures" cannot change the answer, and
    resolving concrete dates only to count them again would be a lossy
    round-trip. Callers that need to *store* the result resolve dates
    separately; the arithmetic never depends on them.

    `rows` is the shape `build_wallet` takes, and is never mutated.
    """
    row = next((r for r in rows if r["id"] == subject_id), None)
    if row is None:
        return []

    headroom = row["remaining"] - row["planned_absences"]
    if headroom <= 0:
        return []

    if max_n is None:
        # Show the cliff plus a step past it. A ladder long enough to reach the
        # end of term is unreadable, and the only interesting rung is the first
        # unsafe one — but the cap has to live here, or the `min(planned,
        # remaining)` clamp downstream would flatten the top rungs silently.
        budget = build_wallet(rows, overall_limit).by_id().get(subject_id)
        max_n = max((budget.budget if budget else 0) + 2, 5)
    limit = min(headroom, max_n, 10)

    # What is already broken before this decision, so each rung can report the
    # cost of itself rather than the state of the world.
    baseline = simulate(rows, overall_limit)
    already_broken = set(baseline.breaks)

    rungs = []
    for n in range(1, limit + 1):
        shifted = [
            {**r, "planned_absences": r["planned_absences"] + n}
            if r["id"] == subject_id else r
            for r in rows
        ]
        result = simulate(shifted, overall_limit)
        plan = result.wallet.by_id()[subject_id]
        new_breaks = tuple(c for c in result.breaks if c not in already_broken)
        newly_overall = result.overall_breaks and not baseline.overall_breaks
        rungs.append(LadderRung(
            n=n,
            subject_pct=plan.projected_worst_pct,
            subject_verdict=plan.verdict,
            subject_budget_left=plan.budget,
            overall_pct=result.wallet.overall_projected_worst_pct,
            overall_verdict=result.wallet.overall_verdict,
            breaks=new_breaks,
            is_safe=not new_breaks and not newly_overall,
        ))
    return rungs
