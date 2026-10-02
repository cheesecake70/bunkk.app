"""Golden-file tests for the detailed-report parser.

Ground truth comes from the real portal PDFs in tests/golden/ and was
independently verified against the portal's own summary reports:
the July aggregates below match the summary_july.pdf figures exactly
(Conducted = P + A, NU identical) for all 14 subjects.
"""
from collections import Counter, defaultdict
from datetime import date, time
from pathlib import Path

import pytest

from report_parser import (
    LectureStatus,
    LectureType,
    SummaryFormatError,
    UnrecognisedReportError,
    parse_pdf,
)

GOLDEN = Path(__file__).parent / "golden"

# (canonical_name, lecture_type): (P, A, NU) for July only —
# cross-verified against the portal's July summary report.
JULY_TRUTH = {
    ("Community Engagement Service", LectureType.TUTORIAL): (4, 0, 0),
    ("Computational Mathematics", LectureType.THEORY): (3, 0, 3),
    ("Computer Networks", LectureType.THEORY): (4, 0, 1),
    ("Data Structures Laboratory", LectureType.PRACTICAL): (4, 0, 2),
    ("Data Structures", LectureType.THEORY): (3, 2, 2),
    ("Database Management System Laboratory", LectureType.PRACTICAL): (4, 0, 0),
    ("Database Management System", LectureType.THEORY): (0, 0, 8),
    ("Design Thinking Laboratory", LectureType.TUTORIAL): (2, 2, 2),
    ("Innovative Product Development I", LectureType.UNKNOWN): (2, 0, 2),
    ("Operations Research", LectureType.THEORY): (2, 1, 5),
    ("Python Programming Laboratory", LectureType.PRACTICAL): (0, 0, 4),
    ("Statistics for Data Science", LectureType.THEORY): (2, 0, 6),
    ("Universal Human Values Tutorial", LectureType.TUTORIAL): (2, 0, 1),
    ("Universal Human Values", LectureType.THEORY): (2, 0, 2),
}


@pytest.fixture(scope="module")
def report():
    return parse_pdf(GOLDEN / "detailed_jul_aug.pdf")


class TestHeader:
    def test_identity(self, report):
        h = report.header
        assert h.student_name == "SOHAM NONDA"
        assert h.student_number == "60000000001"
        assert h.roll_no == "C000"
        assert h.academic_session == "2026-2027, Semester III"
        assert h.program == "B.Tech in Computer Engineering"

    def test_duration(self, report):
        assert report.header.period_start == date(2026, 7, 1)
        assert report.header.period_end == date(2026, 8, 12)


class TestRows:
    def test_row_count(self, report):
        assert len(report.lectures) == 126

    def test_subject_count(self, report):
        assert len(report.course_keys) == 14

    def test_first_and_last_rows(self, report):
        first, last = report.lectures[0], report.lectures[-1]
        assert first.canonical_name == "Computer Networks"
        assert first.on_date == date(2026, 7, 16)
        assert first.start_time == time(10, 0, 1)
        assert first.status is LectureStatus.NOT_UPDATED
        assert last.canonical_name == "Community Engagement Service"
        assert last.on_date == date(2026, 8, 12)
        assert last.status is LectureStatus.NOT_UPDATED

    def test_all_dates_within_duration(self, report):
        h = report.header
        assert all(h.period_start <= l.on_date <= h.period_end for l in report.lectures)

    def test_same_slot_clash_preserved(self, report):
        """Jul 16 1PM has two genuine rows (DBMS Theory + DS Theory) — both must survive."""
        clash = [
            l for l in report.lectures
            if l.on_date == date(2026, 7, 16) and l.start_time == time(13, 0, 1)
        ]
        assert {l.canonical_name for l in clash} == {
            "Database Management System", "Data Structures",
        }


class TestJulyAggregatesMatchSummaryReport:
    """The detailed report folded to July must equal the portal's July summary."""

    def test_all_14_subjects(self, report):
        agg = defaultdict(Counter)
        for l in report.lectures:
            if l.on_date.month == 7:
                agg[(l.canonical_name, l.lecture_type)][l.status] += 1

        assert set(agg) == set(JULY_TRUTH)
        for key, (p, a, nu) in JULY_TRUTH.items():
            c = agg[key]
            assert (
                c[LectureStatus.PRESENT],
                c[LectureStatus.ABSENT],
                c[LectureStatus.NOT_UPDATED],
            ) == (p, a, nu), f"aggregate mismatch for {key}"


class TestNormalization:
    def test_lab_short_form_merges_with_laboratory(self, report):
        """'Database Management System Lab C22' must normalise to the Laboratory subject."""
        names = {l.raw_course_name for l in report.lectures
                 if l.canonical_name == "Database Management System Laboratory"}
        assert "Database Management System Lab C22" in names

    def test_theory_and_lab_are_distinct_subjects(self, report):
        keys = report.course_keys
        assert "Database Management System|Theory" in keys
        assert "Database Management System Laboratory|Practical" in keys

    def test_suggested_codes(self, report):
        codes = {l.course_key: l.suggested_code for l in report.lectures}
        assert codes["Computer Networks|Theory"] == "CN"
        assert codes["Database Management System|Theory"] == "DBMS"
        assert codes["Database Management System Laboratory|Practical"] == "DBMS Lab"
        assert codes["Universal Human Values Tutorial|Tutorial"] == "UHV Tut"
        assert codes["Operations Research|Theory"] == "OR"


class TestWrongInputs:
    def test_summary_pdf_redirects_politely(self):
        with pytest.raises(SummaryFormatError) as e:
            parse_pdf(GOLDEN / "summary_july.pdf")
        assert "detailed" in str(e.value).lower()

    def test_random_pdf_rejected(self, tmp_path):
        # A structurally-valid but non-report PDF.
        from reportlab_stub import make_blank_pdf  # local helper

        blank = tmp_path / "blank.pdf"
        make_blank_pdf(blank, text="Grocery list: eggs, milk")
        with pytest.raises(UnrecognisedReportError):
            parse_pdf(blank)

    def test_bytes_input_works(self):
        data = (GOLDEN / "detailed_jul_aug.pdf").read_bytes()
        report = parse_pdf(data)
        assert len(report.lectures) == 126
