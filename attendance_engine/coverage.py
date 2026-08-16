"""Coverage, gaps and staleness — implementation-plan §3.2 rule 5.

The ledger is only as trustworthy as the date ranges it has actually seen. This
module answers three questions from the list of merged report windows:

  a) coverage — is there a stretch of the semester no report has ever covered?
  b) staleness — how long since the newest covered day?
  c) pending — how old is the oldest unresolved (NU) lecture, and what range
     would the user have to export to resolve it?

Pure date arithmetic; no DB, no Flask.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def __str__(self) -> str:  # "13.08 → 21.08" reads like the portal's own UI
        return f"{self.start:%d.%m} → {self.end:%d.%m}"


@dataclass(frozen=True)
class CoverageReport:
    covered: list[DateRange]        # merged, sorted
    gaps: list[DateRange]           # holes *inside* the covered span
    trailing_gap: DateRange | None  # newest covered day → today
    latest_covered: date | None
    stale_days: int                 # 0 when a report covers today
    is_stale: bool
    pending_count: int
    oldest_pending: date | None
    #: The single range that fixes everything: semester start → today. Bunkmate
    #: only ever asks for one habit, so every problem has the same one-tap answer.
    suggested_export: DateRange | None

    @property
    def has_gaps(self) -> bool:
        return bool(self.gaps)


def merge_ranges(ranges: list[DateRange]) -> list[DateRange]:
    """Union of date ranges. Ranges touching end-to-start count as contiguous."""
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda r: (r.start, r.end))
    merged = [ordered[0]]
    for nxt in ordered[1:]:
        last = merged[-1]
        if nxt.start <= last.end + timedelta(days=1):
            if nxt.end > last.end:
                merged[-1] = DateRange(last.start, nxt.end)
        else:
            merged.append(nxt)
    return merged


def find_gaps(covered: list[DateRange]) -> list[DateRange]:
    """Uncovered stretches strictly between covered ranges."""
    return [
        DateRange(a.end + timedelta(days=1), b.start - timedelta(days=1))
        for a, b in zip(covered, covered[1:])
    ]


def analyse(
    ranges: list[DateRange],
    *,
    today: date | None = None,
    staleness_days: int = 7,
    pending_count: int = 0,
    oldest_pending: date | None = None,
) -> CoverageReport:
    today = today or date.today()
    covered = merge_ranges(ranges)

    if not covered:
        return CoverageReport(
            covered=[], gaps=[], trailing_gap=None, latest_covered=None,
            stale_days=0, is_stale=False,
            pending_count=pending_count, oldest_pending=oldest_pending,
            suggested_export=None,
        )

    latest = covered[-1].end
    stale_days = max(0, (today - latest).days)
    trailing = (
        DateRange(latest + timedelta(days=1), today) if latest < today else None
    )

    return CoverageReport(
        covered=covered,
        gaps=find_gaps(covered),
        trailing_gap=trailing,
        latest_covered=latest,
        stale_days=stale_days,
        is_stale=stale_days > staleness_days,
        pending_count=pending_count,
        oldest_pending=oldest_pending,
        suggested_export=DateRange(covered[0].start, max(latest, today)),
    )
