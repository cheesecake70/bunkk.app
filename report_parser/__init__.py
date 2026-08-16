"""report_parser — pure package turning college attendance PDFs into typed data.

Public API:
    parse_pdf(path_or_bytes) -> ParsedReport
    Raises ReportParseError subclasses on anything suspicious.

No Flask, no database imports. Keep it that way.
"""
from .types import (
    Lecture,
    LectureStatus,
    LectureType,
    ParsedReport,
    ReportHeader,
    ReportParseError,
    SummaryFormatError,
    UnrecognisedReportError,
    ValidationError,
)
from .detailed import parse_pdf

__all__ = [
    "parse_pdf",
    "ParsedReport",
    "ReportHeader",
    "Lecture",
    "LectureStatus",
    "LectureType",
    "ReportParseError",
    "SummaryFormatError",
    "UnrecognisedReportError",
    "ValidationError",
]
