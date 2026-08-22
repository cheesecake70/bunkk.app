"""Merge-engine tests — one per rule in implementation-plan §3.2.

The end-to-end case ingests the real portal PDF and asserts the ledger ends up
holding exactly the 126 lectures the parser's golden tests already pin down;
the synthetic cases cover the lifecycle a single PDF can't show (NU resolving,
new days arriving, rows vanishing).
"""
from datetime import date, time
from pathlib import Path

import pytest

from app import create_app, db
from app.merge import ingest, ingest_report
from app.models import (
    CourseAlias,
    LectureChange,
    LectureInstance,
    ReportSnapshot,
    Subject,
    User,
)
from app.services import coverage_for, dashboard_for
from attendance_engine import Verdict
from report_parser import parse_pdf
from report_parser.types import (
    Lecture,
    LectureStatus,
    LectureType,
    ParsedReport,
    ReportHeader,
)

GOLDEN = Path(__file__).parent / "golden" / "detailed_jul_aug.pdf"


@pytest.fixture()
def app(tmp_path):
    app = create_app("config.TestConfig")
    app.config["UPLOAD_DIR"] = str(tmp_path / "uploads")
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def user(app):
    user = User(email="m@example.com", username="mokssha")
    user.set_password("x")
    db.session.add(user)
    db.session.commit()
    return user


def header(start=date(2026, 7, 1), end=date(2026, 8, 12), number="60004250098"):
    return ReportHeader(
        student_name="MOKSSHA NANDU",
        student_number=number,
        roll_no="C101",
        academic_session="2026-2027, Semester III",
        program="B.Tech in Computer Engineering",
        period_start=start,
        period_end=end,
    )


def lecture(name="Computer Networks", on=date(2026, 7, 16), start=time(10, 0, 1),
            status=LectureStatus.NOT_UPDATED, ltype=LectureType.THEORY, code="CN"):
    return Lecture(
        raw_course_name=f"{name}T C2",
        canonical_name=name,
        lecture_type=ltype,
        suggested_code=code,
        on_date=on,
        start_time=start,
        end_time=time(start.hour + 1, 0, 0),
        status=status,
    )


def send(user, lectures, *, digest, start=date(2026, 7, 1), end=date(2026, 8, 12)):
    return ingest_report(
        user, ParsedReport(header=header(start, end), lectures=lectures), digest=digest
    )


# ---------------------------------------------------------------------------


class TestRealPdfEndToEnd:
    def test_first_upload_builds_the_ledger(self, user):
        result = ingest(user, GOLDEN.read_bytes(), "report.pdf")

        assert result.status == "merged"
        assert result.added == 126
        assert result.updated == 0
        assert db.session.query(LectureInstance).count() == 126
        assert db.session.query(Subject).count() == 14

    def test_subjects_get_readable_codes(self, user):
        ingest(user, GOLDEN.read_bytes(), "report.pdf")
        codes = {s.code for s in db.session.query(Subject).all()}
        assert {"CN", "DBMS", "DBMS Lab", "OR", "UHV Tut"} <= codes

    def test_every_raw_name_is_remembered_as_an_alias(self, user):
        ingest(user, GOLDEN.read_bytes(), "report.pdf")
        raw_names = {a.raw_name for a in db.session.query(CourseAlias).all()}
        assert "Computer NetworksT C2" in raw_names
        assert "Database Management System Lab C22" in raw_names

    def test_identical_file_is_a_no_op(self, user):
        """Rule 3, strongest form — a byte-identical re-upload changes nothing."""
        data = GOLDEN.read_bytes()
        ingest(user, data, "report.pdf")
        before = dashboard_for(user)

        again = ingest(user, data, "report.pdf")

        assert again.status == "duplicate"
        assert db.session.query(LectureInstance).count() == 126
        assert db.session.query(ReportSnapshot).count() == 1
        after = dashboard_for(user)
        assert [(s.code, s.worst_pct) for s in after.subjects] == \
               [(s.code, s.worst_pct) for s in before.subjects]

    def test_dashboard_math_over_the_real_ledger(self, user):
        """DS Theory across the whole 01.07–12.08 window: 7 present, 2 absent, 3 pending.

        So C=9, T=12 — official 7/9 = 77.8%, worst 7/12 = 58.3%. Worst case is
        below the 70% rule, so the subject must read DANGER with no budget.
        """
        ingest(user, GOLDEN.read_bytes(), "report.pdf")
        ds = next(s for s in dashboard_for(user).subjects if s.code == "DS")

        assert (ds.counts.present, ds.counts.absent, ds.counts.unknown) == (7, 2, 3)
        assert ds.official_pct == pytest.approx(700 / 9)
        assert ds.worst_pct == pytest.approx(700 / 12)
        assert ds.best_pct == pytest.approx(1000 / 12)
        assert ds.verdict is Verdict.DANGER
        assert ds.can_miss == 0
        assert ds.recover_needed == 5      # 7+5 / 12+5 = 12/17 = 70.6% ✓

    def test_ledger_matches_the_parser_row_for_row(self, user):
        ingest(user, GOLDEN.read_bytes(), "report.pdf")
        parsed = parse_pdf(GOLDEN)
        ledger = {
            (l.subject.canonical_name, l.on_date, l.start_time, l.status)
            for l in db.session.query(LectureInstance).all()
        }
        expected = {
            (l.canonical_name, l.on_date, l.start_time, l.status.value)
            for l in parsed.lectures
        }
        assert ledger == expected

    def test_rejects_another_students_report(self, app):
        other = User(email="other@example.com", username="other", student_number="99999999999")
        other.set_password("x")
        db.session.add(other)
        db.session.commit()

        from app.merge import MergeError

        with pytest.raises(MergeError, match="belongs to student"):
            ingest(other, GOLDEN.read_bytes(), "report.pdf")

    def test_first_upload_adopts_the_identity_on_the_report(self, user):
        ingest(user, GOLDEN.read_bytes(), "report.pdf")
        assert user.student_number == "60004250098"
        assert user.roll_no == "C101"


class TestRule1StatusChanges:
    def test_pending_resolving_is_recorded_and_surfaced(self, user):
        send(user, [lecture(status=LectureStatus.NOT_UPDATED)], digest="a")
        result = send(user, [lecture(status=LectureStatus.PRESENT)], digest="b")

        assert result.added == 0
        assert result.updated == 1
        assert result.resolved_pending == 1
        change = result.changes[0]
        assert (change.from_status, change.to_status) == ("NU", "P")

        logged = db.session.query(LectureChange).one()
        assert (logged.from_status, logged.to_status) == ("NU", "P")
        assert db.session.query(LectureInstance).one().status == "P"

    def test_percentage_movement_is_reported(self, user):
        send(user, [
            lecture(on=date(2026, 7, 16), status=LectureStatus.NOT_UPDATED),
            lecture(on=date(2026, 7, 17), status=LectureStatus.PRESENT),
        ], digest="a")
        result = send(user, [
            lecture(on=date(2026, 7, 16), status=LectureStatus.PRESENT),
            lecture(on=date(2026, 7, 17), status=LectureStatus.PRESENT),
        ], digest="b")

        before, after = result.pct_moves["CN"]
        assert before == 50.0    # worst case: 1 of 2 known present
        assert after == 100.0

    def test_a_correction_is_also_a_change(self, user):
        send(user, [lecture(status=LectureStatus.PRESENT)], digest="a")
        result = send(user, [lecture(status=LectureStatus.ABSENT)], digest="b")
        assert result.changes[0].from_status == "P"
        assert result.resolved_pending == 0


class TestRule2NewLectures:
    def test_new_days_are_added_and_old_ones_left_alone(self, user):
        send(user, [lecture(on=date(2026, 7, 16))], digest="a")
        result = send(
            user,
            [lecture(on=date(2026, 7, 16)), lecture(on=date(2026, 7, 23))],
            digest="b",
            end=date(2026, 7, 23),
        )
        assert (result.added, result.unchanged, result.updated) == (1, 1, 0)
        assert db.session.query(LectureInstance).count() == 2

    def test_same_slot_clash_is_two_lectures_not_one(self, user):
        """Jul 16 1PM genuinely holds both DBMS Theory and DS Theory."""
        result = send(user, [
            lecture(name="Database Management System", start=time(13, 0, 1), code="DBMS"),
            lecture(name="Data Structures", start=time(13, 0, 1), code="DS"),
        ], digest="a")
        assert result.added == 2
        assert db.session.query(LectureInstance).count() == 2


class TestRule4Vanished:
    def test_row_missing_from_a_covering_report_is_flagged_not_deleted(self, user):
        send(user, [
            lecture(on=date(2026, 7, 16)),
            lecture(on=date(2026, 7, 17)),
        ], digest="a")

        result = send(user, [lecture(on=date(2026, 7, 16))], digest="b")

        assert len(result.vanished) == 1
        assert result.vanished[0].on_date == date(2026, 7, 17)
        assert db.session.query(LectureInstance).count() == 2   # nothing deleted
        gone = db.session.query(LectureInstance).filter_by(
            on_date=date(2026, 7, 17)).one()
        assert gone.is_vanished

    def test_vanished_rows_leave_the_totals(self, user):
        send(user, [
            lecture(on=date(2026, 7, 16), status=LectureStatus.PRESENT),
            lecture(on=date(2026, 7, 17), status=LectureStatus.ABSENT),
        ], digest="a")
        assert dashboard_for(user).overall.counts.total == 2

        send(user, [lecture(on=date(2026, 7, 16), status=LectureStatus.PRESENT)],
             digest="b")
        assert dashboard_for(user).overall.counts.total == 1

    def test_outside_the_report_window_nothing_vanishes(self, user):
        """A June report says nothing about July — absence there proves nothing."""
        send(user, [lecture(on=date(2026, 7, 16))], digest="a")
        send(user, [lecture(on=date(2026, 6, 3))],
             digest="b", start=date(2026, 6, 1), end=date(2026, 6, 30))

        assert not db.session.query(LectureInstance).filter_by(
            on_date=date(2026, 7, 16)).one().is_vanished

    def test_a_returning_row_is_unflagged(self, user):
        send(user, [lecture(on=date(2026, 7, 16)), lecture(on=date(2026, 7, 17))],
             digest="a")
        send(user, [lecture(on=date(2026, 7, 16))], digest="b")
        send(user, [lecture(on=date(2026, 7, 16)), lecture(on=date(2026, 7, 17))],
             digest="c")

        assert not db.session.query(LectureInstance).filter_by(
            on_date=date(2026, 7, 17)).one().is_vanished


class TestRule5Coverage:
    def test_gap_between_two_uploads_is_found(self, user):
        send(user, [lecture(on=date(2026, 7, 16))],
             digest="a", start=date(2026, 7, 1), end=date(2026, 8, 12))
        send(user, [lecture(on=date(2026, 8, 25))],
             digest="b", start=date(2026, 8, 22), end=date(2026, 8, 31))

        report = coverage_for(user, today=date(2026, 8, 31))
        assert [str(g) for g in report.gaps] == ["13.08 → 21.08"]

    def test_staleness_names_the_range_to_export(self, user):
        send(user, [lecture(on=date(2026, 7, 16))], digest="a")
        report = coverage_for(user, today=date(2026, 8, 30))

        assert report.is_stale
        assert report.stale_days == 18
        assert str(report.suggested_export) == "01.07 → 30.08"

    def test_pending_pile_is_counted_and_dated(self, user):
        send(user, [
            lecture(on=date(2026, 7, 16), status=LectureStatus.NOT_UPDATED),
            lecture(on=date(2026, 7, 17), status=LectureStatus.NOT_UPDATED),
            lecture(on=date(2026, 7, 18), status=LectureStatus.PRESENT),
        ], digest="a")

        report = coverage_for(user, today=date(2026, 8, 12))
        assert report.pending_count == 2
        assert report.oldest_pending == date(2026, 7, 16)


class TestRule6Aliases:
    def test_a_near_miss_asks_once_and_then_remembers(self, user):
        send(user, [lecture(name="Computer Networks")], digest="a")

        # The portal starts printing a slightly different spelling.
        staged = send(user, [lecture(name="Computer Network")], digest="b")
        assert staged.status == "needs_confirmation"
        proposal = staged.proposals[0]
        assert proposal.match_code == "CN"
        assert proposal.similarity > 0.85
        assert db.session.query(Subject).count() == 1     # nothing created yet

        from app.merge import resolve_proposals

        merged = resolve_proposals(
            user, staged.snapshot_id,
            {proposal.raw_name: f"merge:{proposal.match_subject_id}"},
        )
        assert merged.status == "merged"
        assert db.session.query(Subject).count() == 1     # merged, not split

        # Asked once: the same spelling now resolves silently.
        third = send(user, [lecture(name="Computer Network")], digest="c")
        assert third.status == "merged"
        assert not third.proposals

    def test_declining_the_match_creates_a_separate_subject(self, user):
        send(user, [lecture(name="Computer Networks")], digest="a")
        staged = send(user, [lecture(name="Computer Network")], digest="b")

        from app.merge import resolve_proposals

        resolve_proposals(user, staged.snapshot_id, {staged.proposals[0].raw_name: "new"})
        assert db.session.query(Subject).count() == 2

    def test_an_unrelated_subject_is_added_without_asking(self, user):
        send(user, [lecture(name="Computer Networks")], digest="a")
        result = send(user, [lecture(name="Operations Research", code="OR")], digest="b")

        assert result.status == "merged"
        assert result.new_subjects == ["OR"]

    def test_theory_and_lab_never_merge(self, user):
        """Same course, different lecture type = two independent 70% rules."""
        result = send(user, [
            lecture(name="Data Structures", ltype=LectureType.THEORY, code="DS"),
            lecture(name="Data Structures Laboratory", ltype=LectureType.PRACTICAL,
                    start=time(11, 0, 1), code="DS Lab"),
        ], digest="a")
        assert result.status == "merged"
        assert db.session.query(Subject).count() == 2


class TestSnapshotHistory:
    def test_every_upload_is_kept(self, user):
        send(user, [lecture(status=LectureStatus.NOT_UPDATED)], digest="a")
        send(user, [lecture(status=LectureStatus.PRESENT)], digest="b")

        snapshots = db.session.query(ReportSnapshot).all()
        assert len(snapshots) == 2
        assert all(s.parsed_json for s in snapshots)

    def test_lecture_points_at_the_snapshots_that_touched_it(self, user):
        first = send(user, [lecture(status=LectureStatus.NOT_UPDATED)], digest="a")
        second = send(user, [lecture(status=LectureStatus.PRESENT)], digest="b")

        row = db.session.query(LectureInstance).one()
        assert row.first_seen_snapshot_id == first.snapshot_id
        assert row.last_updated_snapshot_id == second.snapshot_id
