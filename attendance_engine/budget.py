"""The budget math — implementation-plan §4, basic mode.

Every threshold comparison here is exact integer arithmetic:
`100·P >= limit·T`, never `P/T >= limit/100` in floats. A rounding error must
never flip a GO/SKIP verdict, so floats appear only where a number is being
*displayed*, never where it is being *decided*.
"""
from __future__ import annotations

import math
from fractions import Fraction

from .types import Counts, Dashboard, OverallStats, SubjectStats, Verdict

#: Below this many spare lectures a subject is amber rather than green.
WARN_MARGIN = 2


def ratio(numerator: int, denominator: int) -> Fraction | None:
    """Exact percentage as a Fraction, or None when the denominator is zero."""
    if denominator <= 0:
        return None
    return Fraction(numerator * 100, denominator)


def meets_limit(present: int, total: int, limit: int) -> bool:
    """Is `present/total` at or above `limit` percent? Exact, integer-only."""
    if total <= 0:
        return True          # nothing has happened yet — nothing is broken
    return 100 * present >= limit * total


def safe_to_miss(present: int, total: int, limit: int) -> int:
    """Largest k with `present / (total + k) >= limit%`.

    Derivation: 100·P >= L·(T + k)  ⟺  k <= 100·P/L − T.
    So k = floor(100·P / L) − T, clamped at zero.
    """
    if limit <= 0:
        return _UNLIMITED       # no minimum to hold — everything is skippable
    if not meets_limit(present, total, limit):
        return 0
    return max(0, (100 * present) // limit - total)


def recovery_needed(present: int, total: int, limit: int) -> int:
    """Smallest r with `(present + r) / (total + r) >= limit%`.

    Derivation: 100(P+r) >= L(T+r)  ⟺  r·(100 − L) >= L·T − 100·P.
    Returns 0 when already at or above the limit.
    """
    if meets_limit(present, total, limit):
        return 0
    if limit >= 100:
        # A 100% rule can never be recovered once a lecture is missed.
        return _UNREACHABLE
    deficit = limit * total - 100 * present
    return max(0, math.ceil(Fraction(deficit, 100 - limit)))


#: Sentinels kept deliberately large but finite so arithmetic never blows up.
_UNLIMITED = 9999
_UNREACHABLE = 9999


def classify(present: int, total: int, limit: int, can_miss: int) -> Verdict:
    if total <= 0:
        return Verdict.UNKNOWN
    if not meets_limit(present, total, limit):
        return Verdict.DANGER
    if can_miss < WARN_MARGIN:
        return Verdict.WARN
    return Verdict.SAFE


def _percentages(c: Counts) -> tuple[Fraction | None, Fraction | None, Fraction | None]:
    official = ratio(c.present, c.conducted)
    worst = ratio(c.present, c.total)
    best = ratio(c.present + c.unknown, c.total)
    return official, worst, best


def overall_stats(counts: Counts, limit: int) -> OverallStats:
    official, worst, best = _percentages(counts)
    can_miss = safe_to_miss(counts.present, counts.total, limit)
    return OverallStats(
        limit=limit,
        counts=counts,
        official_pct=official,
        worst_pct=worst,
        best_pct=best,
        can_miss=can_miss,
        can_miss_optimistic=safe_to_miss(
            counts.present + counts.unknown, counts.total, limit
        ),
        recover_needed=recovery_needed(counts.present, counts.total, limit),
        verdict=classify(counts.present, counts.total, limit, can_miss),
    )


def subject_stats(
    *,
    subject_id: int,
    code: str,
    canonical_name: str,
    lecture_type: str,
    counts: Counts,
    limit: int,
    overall_can_miss: int,
) -> SubjectStats:
    official, worst, best = _percentages(counts)
    can_miss = safe_to_miss(counts.present, counts.total, limit)
    effective = min(can_miss, overall_can_miss)
    return SubjectStats(
        subject_id=subject_id,
        code=code,
        canonical_name=canonical_name,
        lecture_type=lecture_type,
        limit=limit,
        counts=counts,
        official_pct=official,
        worst_pct=worst,
        best_pct=best,
        can_miss=can_miss,
        can_miss_optimistic=safe_to_miss(
            counts.present + counts.unknown, counts.total, limit
        ),
        can_miss_effective=effective,
        recover_needed=recovery_needed(counts.present, counts.total, limit),
        # The verdict follows the *effective* budget: a subject with slack of
        # its own is still tight if the overall limit is the binding one.
        verdict=classify(counts.present, counts.total, limit, effective),
    )


def build_dashboard(rows: list[dict], overall_limit: int) -> Dashboard:
    """Assemble the basic-mode dashboard.

    `rows` are plain dicts so this module stays free of any DB import:
        {id, code, canonical_name, lecture_type, counts: Counts, limit: int}
    """
    total_counts = Counts()
    for row in rows:
        total_counts = total_counts + row["counts"]

    overall = overall_stats(total_counts, overall_limit)

    subjects = [
        subject_stats(
            subject_id=row["id"],
            code=row["code"],
            canonical_name=row["canonical_name"],
            lecture_type=row["lecture_type"],
            counts=row["counts"],
            limit=row["limit"],
            overall_can_miss=overall.can_miss,
        )
        for row in rows
    ]
    # Worst first: the subject most at risk is the one worth reading.
    subjects.sort(key=lambda s: (s.worst_pct if s.worst_pct is not None else Fraction(10**6)))
    return Dashboard(subjects=subjects, overall=overall)
