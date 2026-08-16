"""Detailed-report adapter: PDF -> ParsedReport (one Lecture per row).

Built and golden-tested against real ZSVKM/SAP portal exports. The PDFs are
text-based; pdfplumber's extract_text is reliable. Multi-page tables repeat a
header block on every page; a legend and disclaimer trail the table.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime

import pdfplumber

from .normalize import normalize_course, suggest_code
from .types import (
    Lecture,
    LectureStatus,
    ParsedReport,
    ReportHeader,
    SummaryFormatError,
    UnrecognisedReportError,
    ValidationError,
)

_DURATION_RE = re.compile(
    r"From\s+(\d{2})\.(\d{2})\.(\d{4})\s+to\s+(\d{2})\.(\d{2})\.(\d{4})"
)

_ROW_RE = re.compile(
    r"""^(?P<sr>\d+)\s+
        (?P<course>.+?)\s+
        (?P<date>[A-Z][a-z]{2}\s\d{1,2},\s\d{4})\s+
        (?P<start>\d{1,2}:\d{2}:\d{2}\s[AP]M)\s+
        (?P<end>\d{1,2}:\d{2}:\d{2}\s[AP]M)\s+
        (?P<status>P|A|AG|L|NU)$""",
    re.VERBOSE,
)

_HEADER_FIELDS = {
    "student_name": re.compile(r"^Student Name\s+(.+)$"),
    "student_number": re.compile(r"^Student Number\s+(.+)$"),
    "roll_no": re.compile(r"^Roll No\.\s+(.+)$"),
    "academic_session": re.compile(r"^Academic Year & Academic Session\s+(.+)$"),
    "program": re.compile(r"^Program Name\s+(.+)$"),
}


def _extract_lines(source) -> list[str]:
    """All text lines of the PDF, in order."""
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    lines: list[str] = []
    with pdfplumber.open(source) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(ln.strip() for ln in text.splitlines() if ln.strip())
    return lines


def parse_pdf(source) -> ParsedReport:
    """Parse a detailed attendance report PDF (path, bytes, or file-like).

    Raises:
        SummaryFormatError      -- valid report but the aggregate format
        UnrecognisedReportError -- not an attendance report at all
        ValidationError         -- parsed but failed consistency checks
    """
    lines = _extract_lines(source)
    joined = "\n".join(lines)

    if "Attendance Report" not in joined:
        raise UnrecognisedReportError(
            "This doesn't look like an attendance report from the college portal."
        )

    # Format detection: the summary format has a 'Lecture Conducted' column.
    if "Lecture" in joined and "Conducted" in joined and "Start Time" not in joined:
        raise SummaryFormatError(
            "This is the summary report. Please export the *detailed* report instead "
            "(Attendance → Detailed report → choose your date range)."
        )
    if "Start Time" not in joined:
        raise UnrecognisedReportError(
            "Couldn't find the lecture table in this PDF."
        )

    header = _parse_header(lines)
    lectures = _parse_rows(lines)

    if not lectures:
        raise ValidationError("The report parsed but contained no lecture rows.")

    _validate(header, lectures)
    return ParsedReport(header=header, lectures=lectures)


def _parse_header(lines: list[str]) -> ReportHeader:
    found: dict[str, str] = {}
    period_start = period_end = None

    for ln in lines[:20]:
        for key, rx in _HEADER_FIELDS.items():
            if key not in found:
                m = rx.match(ln)
                if m:
                    found[key] = m.group(1).strip()
        m = _DURATION_RE.search(ln)
        if m:
            d1, m1, y1, d2, m2, y2 = (int(g) for g in m.groups())
            period_start, period_end = date(y1, m1, d1), date(y2, m2, d2)

    missing = [k for k in _HEADER_FIELDS if k not in found]
    if missing or period_start is None:
        raise ValidationError(
            "The report header is incomplete (missing: "
            + ", ".join(missing + ([] if period_start else ["report duration"]))
            + ")."
        )
    if period_end < period_start:
        raise ValidationError("The report duration is reversed (end before start).")

    return ReportHeader(
        student_name=found["student_name"],
        student_number=found["student_number"],
        roll_no=found["roll_no"],
        academic_session=found["academic_session"],
        program=found["program"],
        period_start=period_start,
        period_end=period_end,
    )


def _parse_rows(lines: list[str]) -> list[Lecture]:
    lectures: list[Lecture] = []
    for ln in lines:
        m = _ROW_RE.match(ln)
        if not m:
            continue
        raw_course = m.group("course").strip()
        canonical, ltype = normalize_course(raw_course)
        lectures.append(
            Lecture(
                raw_course_name=raw_course,
                canonical_name=canonical,
                lecture_type=ltype,
                suggested_code=suggest_code(canonical, ltype),
                on_date=datetime.strptime(m.group("date"), "%b %d, %Y").date(),
                start_time=datetime.strptime(m.group("start"), "%I:%M:%S %p").time(),
                end_time=datetime.strptime(m.group("end"), "%I:%M:%S %p").time(),
                status=LectureStatus(m.group("status")),
            )
        )
    return lectures


def _validate(header: ReportHeader, lectures: list[Lecture]) -> None:
    problems: list[str] = []

    for lec in lectures:
        if not (header.period_start <= lec.on_date <= header.period_end):
            problems.append(
                f"Lecture on {lec.on_date} falls outside the report duration "
                f"{header.period_start}–{header.period_end}."
            )
            break  # one example is enough for the user

    # Duplicate identity check: same (course, date, start) must not repeat.
    seen: set[tuple] = set()
    for lec in lectures:
        key = (lec.course_key, lec.on_date, lec.start_time)
        if key in seen:
            problems.append(
                f"Duplicate row for {lec.canonical_name} on {lec.on_date} at "
                f"{lec.start_time} — the PDF may be corrupted."
            )
            break
        seen.add(key)

    if problems:
        raise ValidationError(" ".join(problems))
