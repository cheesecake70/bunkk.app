"""attendance_engine — pure math for budgets and verdicts.

No Flask, no database, no I/O: plain dataclasses in, plain dataclasses out
(ADR-3). This package and `report_parser` are the app's brain and its biggest
correctness risk, so they stay trivially testable.

Contract (implementation-plan §4): exact integer arithmetic only — a rounding
error must never flip a GO/SKIP verdict.
"""
from .budget import (
    WARN_MARGIN,
    build_dashboard,
    classify,
    meets_limit,
    overall_stats,
    ratio,
    recovery_needed,
    safe_to_miss,
    subject_stats,
)
from .coverage import CoverageReport, DateRange, analyse, find_gaps, merge_ranges
from .plan import (
    DayPlan,
    DayVerdict,
    PlannedLecture,
    SimulationResult,
    SubjectPlan,
    Wallet,
    build_wallet,
    day_plans,
    simulate,
)
from .timetable import (
    CONFIDENT_WEEKS,
    CalendarRules,
    LectureRow,
    Occurrence,
    Slot,
    SlotCandidate,
    count_by_subject,
    expand,
    infer_slots,
)
from .types import Counts, Dashboard, OverallStats, SubjectStats, Verdict

__all__ = [
    # budget
    "WARN_MARGIN",
    "build_dashboard",
    "classify",
    "meets_limit",
    "overall_stats",
    "ratio",
    "recovery_needed",
    "safe_to_miss",
    "subject_stats",
    # coverage
    "CoverageReport",
    "DateRange",
    "analyse",
    "find_gaps",
    "merge_ranges",
    # timetable (Phase 2)
    "CONFIDENT_WEEKS",
    "CalendarRules",
    "LectureRow",
    "Occurrence",
    "Slot",
    "SlotCandidate",
    "count_by_subject",
    "expand",
    "infer_slots",
    # planning (Phase 2)
    "DayPlan",
    "DayVerdict",
    "PlannedLecture",
    "SimulationResult",
    "SubjectPlan",
    "Wallet",
    "build_wallet",
    "day_plans",
    "simulate",
    # types
    "Counts",
    "Dashboard",
    "OverallStats",
    "SubjectStats",
    "Verdict",
]
