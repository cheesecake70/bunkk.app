"""Typed results and errors for report parsing. Pure dataclasses, no dependencies."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, time


class ReportParseError(Exception):
    """Base: this PDF could not be ingested. Message is user-facing."""


class SummaryFormatError(ReportParseError):
    """A valid report, but the summary format — ask the user for the detailed export."""


class UnrecognisedReportError(ReportParseError):
    """Not a recognisable attendance report from the portal."""


class ValidationError(ReportParseError):
    """Parsed, but the contents failed an internal consistency check."""


class LectureStatus(str, enum.Enum):
    PRESENT = "P"
    ABSENT = "A"
    ATTENDANCE_GRANTED = "AG"  # treated as present (presumed; confirm on first sighting)
    LATE = "L"                 # unknown: worst-case absent, best-case present
    NOT_UPDATED = "NU"         # pending


class LectureType(str, enum.Enum):
    THEORY = "Theory"
    PRACTICAL = "Practical"
    TUTORIAL = "Tutorial"
    UNKNOWN = "Unknown"


@dataclass(frozen=True)
class ReportHeader:
    student_name: str
    student_number: str
    roll_no: str
    academic_session: str      # e.g. "2026-2027, Semester III"
    program: str
    period_start: date
    period_end: date


@dataclass(frozen=True)
class Lecture:
    """One real-world lecture occurrence. Identity key: (course_key, date, start_time)."""
    raw_course_name: str       # exactly as printed, e.g. "Computer NetworksT C2"
    canonical_name: str        # normalised, e.g. "Computer Networks"
    lecture_type: LectureType
    suggested_code: str        # e.g. "CN", "DBMS Lab" — user may rename once
    on_date: date
    start_time: time
    end_time: time
    status: LectureStatus

    @property
    def course_key(self) -> str:
        """Stable identity for a subject: canonical name + lecture type."""
        return f"{self.canonical_name}|{self.lecture_type.value}"


@dataclass
class ParsedReport:
    header: ReportHeader
    lectures: list[Lecture] = field(default_factory=list)

    @property
    def course_keys(self) -> set[str]:
        return {lec.course_key for lec in self.lectures}
