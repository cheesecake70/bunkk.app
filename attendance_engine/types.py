"""Typed inputs/outputs for the attendance engine. Pure dataclasses.

Vocabulary (implementation-plan §4):
    P  present   — status P, plus AG (attendance granted, presumed present)
    A  absent    — status A
    N  unknown   — status NU (not updated) plus L (late; worst-case absent)
    C  conducted — P + A  (the lectures the portal has resolved)
    T  total     — C + N  (every lecture that actually happened)

Three readings of every percentage:
    official  P / C        what the portal shows today
    worst     P / T        every unknown turns out absent  ← drives all verdicts
    best      (P + N) / T  every unknown turns out present
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from fractions import Fraction


class Verdict(str, enum.Enum):
    """One colour language, shared with the design system's status tokens."""

    SAFE = "safe"        # above limit with room to spare
    WARN = "warn"        # above limit but < WARN_MARGIN lectures of slack
    DANGER = "danger"    # worst case is below the limit
    UNKNOWN = "unknown"  # nothing recorded yet — not a verdict


@dataclass(frozen=True)
class Counts:
    """Lecture tallies for one subject (or a sum across subjects)."""

    present: int = 0
    absent: int = 0
    unknown: int = 0

    @property
    def conducted(self) -> int:
        """C — lectures the portal has resolved to present/absent."""
        return self.present + self.absent

    @property
    def total(self) -> int:
        """T — every lecture that happened, resolved or not."""
        return self.conducted + self.unknown

    def __add__(self, other: "Counts") -> "Counts":
        return Counts(
            present=self.present + other.present,
            absent=self.absent + other.absent,
            unknown=self.unknown + other.unknown,
        )


@dataclass(frozen=True)
class SubjectStats:
    """Everything the dashboard needs about one (course, lecture type) row.

    `can_miss` is this subject's own headroom; `can_miss_effective` also
    respects the overall limit, because missing any lecture spends from both
    budgets. The UI shows the effective number — it is the one that is safe.
    """

    subject_id: int
    code: str
    canonical_name: str
    lecture_type: str
    limit: int                      # percent, e.g. 70
    counts: Counts

    official_pct: Fraction | None   # None when nothing is conducted yet
    worst_pct: Fraction | None
    best_pct: Fraction | None

    can_miss: int                   # worst-case, this subject's limit only
    can_miss_optimistic: int        # if every pending lecture resolves present
    can_miss_effective: int         # min(can_miss, overall headroom)
    recover_needed: int             # consecutive attendances to climb back
    verdict: Verdict

    @property
    def is_below(self) -> bool:
        return self.verdict is Verdict.DANGER


@dataclass(frozen=True)
class OverallStats:
    limit: int
    counts: Counts
    official_pct: Fraction | None
    worst_pct: Fraction | None
    best_pct: Fraction | None
    can_miss: int
    can_miss_optimistic: int
    recover_needed: int
    verdict: Verdict


@dataclass(frozen=True)
class Dashboard:
    """The whole basic-mode picture."""

    subjects: list[SubjectStats]
    overall: OverallStats

    @property
    def headline_can_miss(self) -> int:
        """Lectures skippable right now without breaking anything."""
        return self.overall.can_miss

    @property
    def subjects_below(self) -> list[SubjectStats]:
        return [s for s in self.subjects if s.is_below]

    @property
    def pending_total(self) -> int:
        return self.overall.counts.unknown
