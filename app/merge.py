"""Ingestion & merge — implementation-plan §3.2.

An upload is append-only (ADR-4): the PDF is stored, a ReportSnapshot records
it immutably, and the LectureLedger is updated by per-lecture upsert:

  1. status changed (NU → P/A, or a correction)  → update + LectureChange row
  2. lecture not in the ledger                   → insert
  3. identical row                               → no-op
  4. in the ledger, missing from a report that covers its date
                                                 → flag "vanished", never delete
  5. coverage/staleness                          → attendance_engine.coverage
  6. unknown course name                         → alias resolution, asked once

Subjects are only ever *added* automatically when there is nothing similar to
confuse them with; a near-match asks the user once and remembers the answer
forever (PRD P0.2).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time

from flask import current_app
from sqlalchemy.exc import IntegrityError

from report_parser import Lecture, LectureStatus, ParsedReport, parse_pdf

from . import db
from .models import (
    CourseAlias,
    LectureChange,
    LectureInstance,
    ReportSnapshot,
    Semester,
    Subject,
    User,
)

#: Above this name similarity we suspect two spellings of one subject and ask.
#: Below it, a new subject is created without bothering the user.
SIMILARITY_ASK = 0.85


class MergeError(Exception):
    """User-facing ingestion failure."""


class IdentityClaimed(MergeError):
    """The report's student number already backs a different account.

    Carries who holds it, because "some other account has this" is a dead end:
    the person hitting it is nearly always themselves, signed into the wrong
    one of their two accounts, and the only thing they need is to be told which.

    The email is masked to its first letter and domain. That is enough to tell
    your Gmail account from your Outlook one, and stops a report that fell into
    someone else's hands from also handing over a full contact address.
    """

    def __init__(self, message: str, *, username: str, email: str,
                 student_number: str):
        super().__init__(message)
        self.username = username
        self.email_hint = mask_email(email)
        self.student_number = student_number


def mask_email(email: str) -> str:
    """nandu@gmail.com -> n••••@gmail.com"""
    name, at, domain = (email or "").partition("@")
    if not at or not name:
        return "•••"
    return name[0] + "•" * max(3, len(name) - 1) + "@" + domain


# --------------------------------------------------------------------------
# Result types (plain data — the API layer turns these into JSON)
# --------------------------------------------------------------------------


@dataclass
class StatusChange:
    subject_code: str
    on_date: date
    start_time: time
    from_status: str
    to_status: str


@dataclass
class AliasProposal:
    """'Is this the same subject?' — asked at most once per raw name."""

    raw_name: str
    canonical_name: str
    lecture_type: str
    suggested_code: str
    match_subject_id: int
    match_code: str
    match_name: str
    similarity: float


@dataclass
class MergeResult:
    status: str                       # merged | duplicate | needs_confirmation
    snapshot_id: int | None = None
    period_start: date | None = None
    period_end: date | None = None
    added: int = 0
    unchanged: int = 0
    changes: list[StatusChange] = field(default_factory=list)
    new_subjects: list[str] = field(default_factory=list)
    vanished: list[StatusChange] = field(default_factory=list)
    proposals: list[AliasProposal] = field(default_factory=list)
    #: subject code -> (worst % before, worst % after), floats for display only
    pct_moves: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)

    @property
    def updated(self) -> int:
        return len(self.changes)

    @property
    def resolved_pending(self) -> int:
        return sum(1 for c in self.changes if c.from_status == "NU")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def ingest(user: User, data: bytes, filename: str | None = None) -> MergeResult:
    """Parse, store and merge an uploaded PDF for `user`."""
    digest = hashlib.sha256(data).hexdigest()

    previous = (
        db.session.query(ReportSnapshot)
        .filter_by(user_id=user.id, file_sha256=digest)
        .filter(ReportSnapshot.status.in_(("merged", "staged")))
        .first()
    )
    if previous is not None:
        # Rule 3 taken to its conclusion: an identical file cannot say anything new.
        return MergeResult(
            status="duplicate",
            snapshot_id=previous.id,
            period_start=previous.period_start,
            period_end=previous.period_end,
        )

    report = parse_pdf(data)              # raises ReportParseError subclasses
    return ingest_report(user, report, digest=digest, data=data, filename=filename)


def ingest_report(
    user: User,
    report: ParsedReport,
    *,
    digest: str,
    data: bytes | None = None,
    filename: str | None = None,
) -> MergeResult:
    """Merge an already-parsed report. Split out of `ingest` so merge scenarios
    can be exercised without hand-crafting a PDF for every case."""
    try:
        return _ingest_report(user, report, digest=digest, data=data,
                              filename=filename)
    except IntegrityError:
        # Two accounts uploading the same student's report at the same moment
        # both passed `_check_identity`; the unique index caught the second.
        # Answer it the way the check would have, rather than with a 500.
        db.session.rollback()
        claimed = (
            db.session.query(User)
            .filter(User.student_number == report.header.student_number,
                    User.id != user.id)
            .first()
        )
        if claimed is None:
            raise MergeError("That upload clashed with another; try again.")
        raise IdentityClaimed(
            f"Student {report.header.student_number} is already set up on "
            "another Bunkr account. If that's you, sign in as that account "
            "instead of uploading the report here.",
            username=claimed.username, email=claimed.email,
            student_number=report.header.student_number,
        )


def _ingest_report(user, report, *, digest, data, filename) -> MergeResult:
    _check_identity(user, report)

    semester = _get_or_create_semester(user, report.header.academic_session)

    path = _store_pdf(user, data, digest, filename) if data is not None else "(unsaved)"
    snapshot = ReportSnapshot(
        user_id=user.id,
        semester_id=semester.id,
        period_start=report.header.period_start,
        period_end=report.header.period_end,
        file_path=path,
        original_filename=filename,
        file_sha256=digest,
        lecture_count=len(report.lectures),
        status="staged",
        parsed_json=_serialise(report),
    )
    db.session.add(snapshot)
    db.session.flush()

    mapping, proposals, created = _resolve_subjects(user, semester, report)
    if proposals:
        # Hold the snapshot until the user answers; nothing touches the ledger.
        db.session.commit()
        return MergeResult(
            status="needs_confirmation",
            snapshot_id=snapshot.id,
            period_start=snapshot.period_start,
            period_end=snapshot.period_end,
            proposals=proposals,
        )

    result = _apply(user, snapshot, report, mapping)
    result.new_subjects = created
    db.session.commit()
    return result


def resolve_proposals(user: User, snapshot_id: int, decisions: dict[str, str]) -> MergeResult:
    """Finish a staged upload once the user has answered the alias questions.

    `decisions` maps a raw course name to either "merge:<subject_id>" or "new".
    """
    snapshot = (
        db.session.query(ReportSnapshot)
        .filter_by(id=snapshot_id, user_id=user.id)
        .one_or_none()
    )
    if snapshot is None:
        raise MergeError("That upload no longer exists.")
    if snapshot.status != "staged":
        raise MergeError("That upload has already been processed.")

    report = _deserialise(snapshot.parsed_json)
    semester = _get_or_create_semester(user, report.header.academic_session)

    # Record the user's answers as aliases first; resolution then finds them.
    for lec in report.lectures:
        choice = decisions.get(lec.raw_course_name)
        if not choice:
            continue
        if choice.startswith("merge:"):
            subject = db.session.get(Subject, int(choice.split(":", 1)[1]))
            if subject is None or subject.user_id != user.id:
                raise MergeError("Unknown subject in your answer.")
            _remember_alias(user, lec.raw_course_name, subject, source="user")
        # "new" needs no alias: resolution will create the subject below.

    mapping, proposals, created = _resolve_subjects(
        user, semester, report, force_create=True
    )
    assert not proposals  # force_create leaves nothing to ask about

    result = _apply(user, snapshot, report, mapping)
    result.new_subjects = created
    db.session.commit()
    return result


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


def _check_identity(user: User, report: ParsedReport) -> None:
    """Refuse someone else's report rather than silently ingesting garbage."""
    header = report.header
    if user.student_number and header.student_number != user.student_number:
        raise MergeError(
            f"This report belongs to student {header.student_number}, "
            f"but your account is {user.student_number}."
        )

    if not user.student_number:
        # A student number identifies one person, so it may back only one
        # account. Without this check, uploading a friend's PDF would silently
        # claim their identity and build a second, diverging copy of their
        # ledger — and their next upload would be refused, not yours.
        claimed = (
            db.session.query(User)
            .filter(User.student_number == header.student_number, User.id != user.id)
            .first()
        )
        if claimed is not None:
            raise IdentityClaimed(
                f"Student {header.student_number} is already set up on another "
                "Bunkr account. If that's you, sign in as that account "
                "instead of uploading the report here.",
                username=claimed.username,
                email=claimed.email,
                student_number=header.student_number,
            )

        # First upload claims the identity printed on the report.
        user.student_number = header.student_number
        user.roll_no = header.roll_no


def _store_pdf(user: User, data: bytes, digest: str, filename: str | None) -> str:
    upload_dir = os.path.join(current_app.config["UPLOAD_DIR"], str(user.id))
    os.makedirs(upload_dir, exist_ok=True)
    name = f"{datetime.now():%Y%m%d-%H%M%S}-{digest[:12]}.pdf"
    path = os.path.join(upload_dir, name)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _get_or_create_semester(user: User, session_label: str) -> Semester:
    semester = (
        db.session.query(Semester)
        .filter_by(user_id=user.id, session_label=session_label)
        .one_or_none()
    )
    if semester is None:
        # A new term's first report retires the old term: everything on screen
        # is scoped to the active semester, and there is exactly one of those.
        db.session.query(Semester).filter_by(user_id=user.id).update(
            {"is_active": False}, synchronize_session=False
        )
        semester = Semester(user_id=user.id, session_label=session_label,
                            is_active=True)
        db.session.add(semester)
        db.session.flush()
        from .cache import drop                     # local: avoids a cycle
        from .services import active_semester
        drop(active_semester)
    return semester


def _remember_alias(user: User, raw_name: str, subject: Subject, source: str) -> None:
    existing = (
        db.session.query(CourseAlias)
        .filter_by(user_id=user.id, raw_name=raw_name)
        .one_or_none()
    )
    if existing is None:
        db.session.add(
            CourseAlias(
                user_id=user.id, raw_name=raw_name,
                subject_id=subject.id, source=source,
            )
        )
    elif existing.subject_id != subject.id:
        existing.subject_id = subject.id
        existing.source = source


def _resolve_subjects(
    user: User,
    semester: Semester,
    report: ParsedReport,
    *,
    force_create: bool = False,
) -> tuple[dict[str, Subject], list[AliasProposal], list[str]]:
    """Map every raw course name in the report to a Subject.

    Returns (mapping, proposals, newly-created codes). When `proposals` is
    non-empty the caller must ask the user before touching the ledger.
    """
    subjects = (
        db.session.query(Subject)
        .filter_by(user_id=user.id, semester_id=semester.id)
        .all()
    )
    by_identity = {(s.canonical_name, s.lecture_type): s for s in subjects}
    aliases = {
        a.raw_name: a
        for a in db.session.query(CourseAlias).filter_by(user_id=user.id).all()
    }

    mapping: dict[str, Subject] = {}
    proposals: list[AliasProposal] = []
    created: list[str] = []
    seen_raw: set[str] = set()

    for lec in report.lectures:
        raw = lec.raw_course_name
        if raw in mapping or raw in seen_raw:
            continue

        # 1) an answer we already have
        alias = aliases.get(raw)
        if alias is not None:
            subject = db.session.get(Subject, alias.subject_id)
            if subject is not None:
                mapping[raw] = subject
                continue

        # 2) exact canonical identity
        identity = (lec.canonical_name, lec.lecture_type.value)
        subject = by_identity.get(identity)
        if subject is not None:
            mapping[raw] = subject
            _remember_alias(user, raw, subject, source="auto")
            continue

        # 3) a near-match worth one question
        candidate, score = _best_match(lec, subjects)
        if candidate is not None and not force_create:
            seen_raw.add(raw)
            proposals.append(
                AliasProposal(
                    raw_name=raw,
                    canonical_name=lec.canonical_name,
                    lecture_type=lec.lecture_type.value,
                    suggested_code=lec.suggested_code,
                    match_subject_id=candidate.id,
                    match_code=candidate.code,
                    match_name=candidate.canonical_name,
                    similarity=score,
                )
            )
            continue

        # 4) genuinely new
        subject = Subject(
            user_id=user.id,
            semester_id=semester.id,
            canonical_name=lec.canonical_name,
            lecture_type=lec.lecture_type.value,
            code=_unique_code(lec.suggested_code, subjects),
        )
        db.session.add(subject)
        db.session.flush()
        subjects.append(subject)
        by_identity[identity] = subject
        mapping[raw] = subject
        created.append(subject.code)
        _remember_alias(user, raw, subject, source="auto")

    return mapping, proposals, created


def _best_match(lec: Lecture, subjects: list[Subject]) -> tuple[Subject | None, float]:
    best, best_score = None, 0.0
    for s in subjects:
        if s.lecture_type != lec.lecture_type.value:
            continue
        score = difflib.SequenceMatcher(
            None, s.canonical_name.lower(), lec.canonical_name.lower()
        ).ratio()
        if score > best_score:
            best, best_score = s, score
    if best_score >= SIMILARITY_ASK:
        return best, best_score
    return None, best_score


def _unique_code(suggested: str, subjects: list[Subject]) -> str:
    taken = {s.code for s in subjects}
    if suggested not in taken:
        return suggested
    for n in range(2, 100):
        candidate = f"{suggested} {n}"
        if candidate not in taken:
            return candidate
    return suggested


def _apply(
    user: User,
    snapshot: ReportSnapshot,
    report: ParsedReport,
    mapping: dict[str, Subject],
) -> MergeResult:
    """The upsert itself. Assumes every raw name in `report` is in `mapping`."""
    from .services import subject_worst_percentages   # local: avoids a cycle

    before = subject_worst_percentages(user)

    existing = {
        (l.subject_id, l.on_date, l.start_time): l
        for l in db.session.query(LectureInstance).filter_by(user_id=user.id).all()
    }

    result = MergeResult(
        status="merged",
        snapshot_id=snapshot.id,
        period_start=snapshot.period_start,
        period_end=snapshot.period_end,
    )
    seen_keys: set[tuple] = set()

    for lec in report.lectures:
        subject = mapping[lec.raw_course_name]
        key = (subject.id, lec.on_date, lec.start_time)
        seen_keys.add(key)
        row = existing.get(key)

        if row is None:                                     # rule 2 — insert
            db.session.add(
                LectureInstance(
                    user_id=user.id,
                    subject_id=subject.id,
                    on_date=lec.on_date,
                    start_time=lec.start_time,
                    end_time=lec.end_time,
                    status=lec.status.value,
                    first_seen_snapshot_id=snapshot.id,
                    last_updated_snapshot_id=snapshot.id,
                )
            )
            result.added += 1
        elif row.status != lec.status.value:                # rule 1 — update
            db.session.add(
                LectureChange(
                    lecture_id=row.id,
                    from_status=row.status,
                    to_status=lec.status.value,
                    snapshot_id=snapshot.id,
                )
            )
            result.changes.append(
                StatusChange(
                    subject_code=subject.code,
                    on_date=lec.on_date,
                    start_time=lec.start_time,
                    from_status=row.status,
                    to_status=lec.status.value,
                )
            )
            row.status = lec.status.value
            row.last_updated_snapshot_id = snapshot.id
            row.is_vanished = False
        else:                                               # rule 3 — no-op
            result.unchanged += 1
            if row.is_vanished:
                row.is_vanished = False

    # Rule 4 — in the ledger, inside this report's window, but not in the report.
    for (subject_id, on_date, start_time), row in existing.items():
        if (subject_id, on_date, start_time) in seen_keys:
            continue
        if not (snapshot.period_start <= on_date <= snapshot.period_end):
            continue
        if not row.is_vanished:
            row.is_vanished = True
            result.vanished.append(
                StatusChange(
                    subject_code=row.subject.code,
                    on_date=on_date,
                    start_time=start_time,
                    from_status=row.status,
                    to_status="—",
                )
            )

    snapshot.status = "merged"
    db.session.flush()

    # The ledger these percentages come from has just changed underneath the
    # request-scoped caches; without this, `after` is handed `before` again and
    # every move reports as zero.
    from .planning import forget_derived            # local: avoids a cycle

    forget_derived()
    after = subject_worst_percentages(user)
    result.pct_moves = {
        code: (before.get(code), after_pct)
        for code, after_pct in after.items()
        if before.get(code) != after_pct
    }
    return result


# --------------------------------------------------------------------------
# Snapshot (de)serialisation — keeps the ledger replayable (ADR-4)
# --------------------------------------------------------------------------


def _serialise(report: ParsedReport) -> str:
    h = report.header
    return json.dumps(
        {
            "header": {
                "student_name": h.student_name,
                "student_number": h.student_number,
                "roll_no": h.roll_no,
                "academic_session": h.academic_session,
                "program": h.program,
                "period_start": h.period_start.isoformat(),
                "period_end": h.period_end.isoformat(),
            },
            "lectures": [
                {
                    "raw_course_name": l.raw_course_name,
                    "canonical_name": l.canonical_name,
                    "lecture_type": l.lecture_type.value,
                    "suggested_code": l.suggested_code,
                    "on_date": l.on_date.isoformat(),
                    "start_time": l.start_time.isoformat(),
                    "end_time": l.end_time.isoformat(),
                    "status": l.status.value,
                }
                for l in report.lectures
            ],
        }
    )


def _deserialise(blob: str) -> ParsedReport:
    from report_parser.types import LectureType, ReportHeader

    raw = json.loads(blob)
    h = raw["header"]
    header = ReportHeader(
        student_name=h["student_name"],
        student_number=h["student_number"],
        roll_no=h["roll_no"],
        academic_session=h["academic_session"],
        program=h["program"],
        period_start=date.fromisoformat(h["period_start"]),
        period_end=date.fromisoformat(h["period_end"]),
    )
    lectures = [
        Lecture(
            raw_course_name=l["raw_course_name"],
            canonical_name=l["canonical_name"],
            lecture_type=LectureType(l["lecture_type"]),
            suggested_code=l["suggested_code"],
            on_date=date.fromisoformat(l["on_date"]),
            start_time=time.fromisoformat(l["start_time"]),
            end_time=time.fromisoformat(l["end_time"]),
            status=LectureStatus(l["status"]),
        )
        for l in raw["lectures"]
    ]
    return ParsedReport(header=header, lectures=lectures)
