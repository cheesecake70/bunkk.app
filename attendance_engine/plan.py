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

from .budget import WARN_MARGIN, ratio
from .types import Counts, Verdict


class DayVerdict(str, enum.Enum):
    SKIP = "skip"        # the whole day fits the budget
    PARTIAL = "partial"  # some of it does
    GO = "go"            # nothing does
    OFF = "off"          # no lectures scheduled


@dataclass(frozen=True)
class PlannedLecture:
    """One projected future lecture."""

    subject_id: int
    code: str
    on_date: date
    start_time: time
    end_time: time


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
) -> list[DayPlan]:
    """Verdict per upcoming day.

    Each day is judged *on its own*: "if the only thing I skip is this day, am I
    still safe?" Budgets are shared across days, so skipping two green days in a
    row is not automatically safe — the simulator is how you commit to more than
    one, and it re-derives the whole wallet.
    """
    budgets = {s.subject_id: s.budget for s in wallet.subjects}
    return [
        _day_plan(day, days.get(day, []), budgets, wallet.overall_budget)
        for day in horizon
    ]


def _day_plan(day: date, lectures: list[PlannedLecture],
              budgets: dict[int, int], overall_budget: int) -> DayPlan:
    if not lectures:
        return DayPlan(on_date=day, lectures=[], verdict=DayVerdict.OFF,
                       reason="No classes scheduled.")

    ordered = sorted(lectures, key=lambda l: l.start_time)

    if _fits(ordered, budgets, overall_budget):
        return DayPlan(
            on_date=day, lectures=ordered, verdict=DayVerdict.SKIP,
            reason=_cost_sentence(ordered) + " — all within budget.",
            skippable_codes=sorted({l.code for l in ordered}),
        )

    # Longest skippable suffix -> "leave after X"; longest prefix -> "arrive at Y".
    leave_after = arrive_at = None
    best_suffix: list[PlannedLecture] = []
    for cut in range(len(ordered)):
        if _fits(ordered[cut:], budgets, overall_budget):
            best_suffix = ordered[cut:]
            leave_after = ordered[cut - 1].end_time if cut > 0 else None
            break

    best_prefix: list[PlannedLecture] = []
    for cut in range(len(ordered), 0, -1):
        if _fits(ordered[:cut], budgets, overall_budget):
            best_prefix = ordered[:cut]
            arrive_at = ordered[cut].start_time if cut < len(ordered) else None
            break

    if not best_suffix and not best_prefix:
        blockers = sorted({l.code for l in ordered if budgets.get(l.subject_id, 0) <= 0})
        reason = (
            "No room left in " + ", ".join(blockers) + "."
            if blockers else "Skipping any of it would break a limit."
        )
        return DayPlan(on_date=day, lectures=ordered, verdict=DayVerdict.GO,
                       reason=reason)

    skippable = best_suffix if len(best_suffix) >= len(best_prefix) else best_prefix
    if leave_after is not None:
        reason = f"Leave after {leave_after:%H:%M} — skips {_cost_sentence(best_suffix)}."
    elif arrive_at is not None:
        reason = f"Arrive by {arrive_at:%H:%M} — skips {_cost_sentence(best_prefix)}."
    else:
        reason = "Part of the day is skippable."

    return DayPlan(
        on_date=day, lectures=ordered, verdict=DayVerdict.PARTIAL, reason=reason,
        leave_after=leave_after, arrive_at=arrive_at,
        skippable_codes=sorted({l.code for l in skippable}),
    )


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
