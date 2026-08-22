"""Database models — implementation-plan §6.2.

Multi-tenant from day 1 (every user-owned table keys on user_id), single user
in practice. SQLite in production for now; SQLAlchemy keeps the Postgres path
open (connection-string change + migrations).
"""
from __future__ import annotations

from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from . import db


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
    name = db.Column(db.String(120))
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

    college = db.relationship("College")

    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)


class Settings(db.Model):
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), primary_key=True)
    overall_limit = db.Column(db.Integer, nullable=False, default=75)
    subject_limit = db.Column(db.Integer, nullable=False, default=70)
    staleness_days = db.Column(db.Integer, nullable=False, default=7)
    advanced_mode = db.Column(db.Boolean, nullable=False, default=False)
    #: Phase 3 — the morning nudge. Hour is local to the server (single-tenant
    #: for now; a per-user timezone joins this when the app opens up).
    notify_enabled = db.Column(db.Boolean, nullable=False, default=False)
    notify_hour = db.Column(db.Integer, nullable=False, default=7)


class PushSubscription(db.Model):
    """One browser's Web Push endpoint. A user may install on several devices."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    endpoint = db.Column(db.String(500), nullable=False, unique=True)
    p256dh = db.Column(db.String(200), nullable=False)
    auth = db.Column(db.String(100), nullable=False)
    user_agent = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_sent_at = db.Column(db.DateTime)

    def as_info(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": self.p256dh, "auth": self.auth},
        }


class Semester(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    session_label = db.Column(db.String(80), nullable=False)  # "2026-2027, Semester III"
    start_date = db.Column(db.Date)      # derived: earliest lecture in ledger
    end_date = db.Column(db.Date)        # the one manual date input (advanced setup)
    is_active = db.Column(db.Boolean, nullable=False, default=True)


class Subject(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    semester_id = db.Column(db.Integer, db.ForeignKey("semester.id"), nullable=False)
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
    semester_id = db.Column(db.Integer, db.ForeignKey("semester.id"), nullable=False)
    valid_from = db.Column(db.Date, nullable=False)
    source = db.Column(db.String(10), nullable=False, default="inferred")  # inferred|manual


class TimetableSlot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    version_id = db.Column(db.Integer, db.ForeignKey("timetable_version.id"), nullable=False, index=True)
    weekday = db.Column(db.Integer, nullable=False)  # 0=Mon .. 6=Sun
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subject.id"), nullable=False)


class Holiday(db.Model):
    """Settings › Holidays list: future no-class days, optional name."""
    id = db.Column(db.Integer, primary_key=True)
    semester_id = db.Column(db.Integer, db.ForeignKey("semester.id"), nullable=False, index=True)
    on_date = db.Column(db.Date, nullable=False)
    name = db.Column(db.String(120))          # "Raksha Bandhan", optional
    kind = db.Column(db.String(10), nullable=False, default="holiday")  # holiday|swap
    swap_weekday = db.Column(db.Integer)      # for kind=swap: which weekday's timetable runs

    __table_args__ = (
        db.UniqueConstraint("semester_id", "on_date", name="uq_holiday_date"),
    )


class PlannedAbsence(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    on_date = db.Column(db.Date, nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subject.id"))  # NULL = whole day
    note = db.Column(db.String(200))
