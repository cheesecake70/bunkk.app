"""Database models — implementation-plan §6.2.

Multi-tenant from day 1 (every user-owned table keys on user_id), single user
in practice. SQLite in production for now; SQLAlchemy keeps the Postgres path
open (connection-string change + migrations).
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from . import db


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class College(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    adapter_id = db.Column(db.String(50), nullable=False, default="zsvkm_detailed_v1")
    default_subject_limit = db.Column(db.Integer, nullable=False, default=70)  # percent
    default_overall_limit = db.Column(db.Integer, nullable=False, default=75)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    #: Chosen at sign-up; how you're known in the app.
    username = db.Column(db.String(32), unique=True, nullable=False, index=True)
    #: Claimed from the first uploaded report. Unique: one student, one account —
    #: otherwise two people could ingest the same report and diverge. NULL until
    #: a report is uploaded, and SQLite allows many NULLs in a unique column.
    student_number = db.Column(db.String(40), unique=True)
    roll_no = db.Column(db.String(20))
    college_id = db.Column(db.Integer, db.ForeignKey("college.id"))
    created_at = db.Column(db.DateTime, default=utcnow)
    #: Brute-force protection; see auth.py for the lockout policy.
    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)
    #: What the session cookie actually holds. SQLite hands a deleted row's id
    #: to the next account created, so a remember-me cookie carrying the id
    #: alone would sign its holder into a stranger's account. A random token
    #: belongs to one account for as long as that account exists, and rotating
    #: it is how a password change signs the other devices out.
    session_token = db.Column(db.String(64), unique=True, nullable=False,
                              index=True, default=new_session_token)

    college = db.relationship("College")

    def get_id(self) -> str:
        """Flask-Login stores this in the session and the remember-me cookie."""
        return self.session_token

    def rotate_session(self) -> None:
        self.session_token = new_session_token()

    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)


class Settings(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    overall_limit = db.Column(db.Integer, nullable=False, default=75)
    subject_limit = db.Column(db.Integer, nullable=False, default=70)
    staleness_days = db.Column(db.Integer, nullable=False, default=7)


class Semester(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    session_label = db.Column(db.String(80), nullable=False)  # "2026-2027, Semester III"
    end_date = db.Column(db.Date)        # the one manual date input (advanced setup)
    #: Exactly one per user is active: the one the newest report named. Every
    #: figure on screen is scoped to it, so last term's lectures stop counting
    #: the moment this term's first report arrives.
    is_active = db.Column(db.Boolean, nullable=False, default=True)


class Subject(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    semester_id = db.Column(db.Integer, db.ForeignKey("semester.id"), nullable=False, index=True)
    canonical_name = db.Column(db.String(200), nullable=False)
    lecture_type = db.Column(db.String(20), nullable=False)   # Theory/Practical/Tutorial/Unknown
    code = db.Column(db.String(20), nullable=False)           # user-approved short name
    custom_limit = db.Column(db.Integer)                      # overrides Settings.subject_limit
    active = db.Column(db.Boolean, nullable=False, default=True)

    __table_args__ = (
        db.UniqueConstraint("semester_id", "canonical_name", "lecture_type",
                            name="uq_subject_identity"),
    )


class CourseAlias(db.Model):
    """raw printed name -> subject, remembered forever (asked at most once)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    raw_name = db.Column(db.String(255), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subject.id"), nullable=False)
    source = db.Column(db.String(10), nullable=False, default="auto")  # auto|user

    __table_args__ = (
        db.UniqueConstraint("user_id", "raw_name", name="uq_alias_per_user"),
    )


class ReportSnapshot(db.Model):
    """Immutable record of every upload (event-sourcing lite, ADR-4)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    #: The semester the report's header named. Coverage and staleness are
    #: judged per semester, so July's report can't make September look covered.
    semester_id = db.Column(db.Integer, db.ForeignKey("semester.id"), index=True)
    uploaded_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)
    file_path = db.Column(db.String(500), nullable=False)   # raw PDF, private dir
    original_filename = db.Column(db.String(255))
    file_sha256 = db.Column(db.String(64), nullable=False, index=True)
    lecture_count = db.Column(db.Integer, nullable=False)
    # staged = parsed and stored, waiting on the user's alias decisions.
    status = db.Column(db.String(20), nullable=False, default="merged")  # merged|staged|duplicate|error
    #: Parsed rows exactly as the adapter produced them. Keeping these makes the
    #: ledger a replayable fold over snapshots (ADR-4) without re-reading PDFs.
    parsed_json = db.Column(db.Text)


class LectureInstance(db.Model):
    """Canonical ledger: one row per real-world lecture. Key: (subject, date, start)."""
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    subject_id = db.Column(db.Integer, db.ForeignKey("subject.id"), nullable=False, index=True)
    on_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    status = db.Column(db.String(3), nullable=False)  # P|A|AG|L|NU
    first_seen_snapshot_id = db.Column(db.Integer, db.ForeignKey("report_snapshot.id"), nullable=False)
    last_updated_snapshot_id = db.Column(db.Integer, db.ForeignKey("report_snapshot.id"), nullable=False)
    #: Present in the ledger but absent from a later report covering its date —
    #: possibly cancelled or corrected upstream. Flagged for review, never
    #: silently deleted (implementation-plan §3.2 rule 4).
    is_vanished = db.Column(db.Boolean, nullable=False, default=False)

    subject = db.relationship("Subject")

    __table_args__ = (
        db.UniqueConstraint("subject_id", "on_date", "start_time",
                            name="uq_lecture_identity"),
    )


class LectureChange(db.Model):
    """Status transition log (NU -> P etc.), powers upload diffs."""
    id = db.Column(db.Integer, primary_key=True)
    lecture_id = db.Column(db.Integer, db.ForeignKey("lecture_instance.id"), nullable=False, index=True)
    from_status = db.Column(db.String(3), nullable=False)
    to_status = db.Column(db.String(3), nullable=False)
    snapshot_id = db.Column(db.Integer, db.ForeignKey("report_snapshot.id"), nullable=False)
    changed_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class TimetableVersion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    semester_id = db.Column(db.Integer, db.ForeignKey("semester.id"), nullable=False, index=True)
    valid_from = db.Column(db.Date, nullable=False)
    source = db.Column(db.String(10), nullable=False, default="inferred")  # inferred|manual


class TimetableSlot(db.Model):
    """One block in the weekly grid — a class, or a break between classes.

    Breaks carry no subject and never reach the attendance maths: `active_slots`
    filters on `kind` before building the engine's `Slot` list, so a free period
    cannot change how many lectures are left. They exist so the grid looks like
    the day actually looks, which is what makes "leave after 2pm" believable.
    """

    id = db.Column(db.Integer, primary_key=True)
    version_id = db.Column(db.Integer, db.ForeignKey("timetable_version.id"), nullable=False, index=True)
    weekday = db.Column(db.Integer, nullable=False)  # 0=Mon .. 6=Sun
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    kind = db.Column(db.String(10), nullable=False, default="class")   # class|break
    #: NULL for a break.
    subject_id = db.Column(db.Integer, db.ForeignKey("subject.id"))
    label = db.Column(db.String(60))          # "Lunch", breaks only

    subject = db.relationship("Subject")


class Holiday(db.Model):
    """Settings › Holidays list: future no-class days, optional name."""
    id = db.Column(db.Integer, primary_key=True)
    semester_id = db.Column(db.Integer, db.ForeignKey("semester.id"), nullable=False, index=True)
    on_date = db.Column(db.Date, nullable=False)
    name = db.Column(db.String(120))          # "Raksha Bandhan", optional

    __table_args__ = (
        db.UniqueConstraint("semester_id", "on_date", name="uq_holiday_date"),
    )


class LecturePrediction(db.Model):
    """Your guess at how an unmarked lecture will resolve.

    Guesses feed the real numbers: a lecture you mark "probably present" counts
    as present everywhere, because that is usually the truth and worst-case
    arithmetic over a big pile of NU makes the app useless in the meantime.

    The trade is stated plainly in the UI, because it is real: a wrong guess can
    turn a genuine DANGER into a green verdict. Two things keep it honest —
    every guessed lecture is badged wherever it moves a number, and Overview
    always carries the untouched worst case beside it.

    Keyed on the lecture, and read through a join that requires the status to
    still be unknown, so a fresh report retires the guess automatically without
    anything having to delete it.
    """

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    lecture_id = db.Column(db.Integer, db.ForeignKey("lecture_instance.id"),
                           nullable=False, index=True)
    predicted = db.Column(db.String(1), nullable=False)        # P | A
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    __table_args__ = (
        db.UniqueConstraint("user_id", "lecture_id", name="uq_prediction_per_lecture"),
    )


class Checkpoint(db.Model):
    """A date the college actually audits attendance on.

    It carries no percentage of its own — an audit applies the same limits as
    everything else. What a checkpoint changes is the *horizon*, not the bar:
    lectures after it can't help you clear it, so they drop out of the maths
    until it passes.

    The semester end is an implicit final checkpoint and is deliberately not
    stored here; `Semester.end_date` stays its single source of truth, so the
    two can never drift apart and deleting the last row can't leave the app
    without a horizon.
    """

    id = db.Column(db.Integer, primary_key=True)
    semester_id = db.Column(db.Integer, db.ForeignKey("semester.id"),
                            nullable=False, index=True)
    on_date = db.Column(db.Date, nullable=False)
    label = db.Column(db.String(120))          # "Mid-sem audit", optional

    __table_args__ = (
        db.UniqueConstraint("semester_id", "on_date", name="uq_checkpoint_date"),
    )


class PlannedAbsence(db.Model):
    """A future lecture you have decided to miss.

    Identity is three-tier, so the same day can hold as much or as little
    detail as the decision actually had:

        subject_id NULL, start_time NULL  ->  the whole day
        subject_id set,  start_time NULL  ->  every lecture of that subject
        subject_id set,  start_time set   ->  exactly that one lecture

    The third tier exists because a timetable really can run two lectures of
    one subject in a day, and being able to plan only one of them is not a
    detail the app gets to round off.
    """

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    on_date = db.Column(db.Date, nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subject.id"))
    start_time = db.Column(db.Time)
    note = db.Column(db.String(200))

    subject = db.relationship("Subject")

    __table_args__ = (
        db.UniqueConstraint("user_id", "on_date", "subject_id", "start_time",
                            name="uq_planned_absence"),
    )
