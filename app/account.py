"""Account: profile, invites, data export and deletion.

Export and delete aren't nice-to-haves once the app holds other people's
attendance (§6.4). Someone who joins on a friend's server should be able to
take their data and leave without asking anyone.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone

from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    json,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required, logout_user

from . import db
from .auth import new_invite_code, registration_mode
from .models import (
    CourseAlias,
    Holiday,
    Invite,
    LectureChange,
    LectureInstance,
    PlannedAbsence,
    PushSubscription,
    ReportSnapshot,
    Semester,
    Settings,
    Subject,
    TimetableSlot,
    TimetableVersion,
    User,
)

bp = Blueprint("account", __name__, url_prefix="/account")

#: How many unused invites one person may hold at a time.
INVITE_LIMIT = 10


@bp.get("/")
@login_required
def index():
    invites = (
        db.session.query(Invite)
        .filter_by(created_by_id=current_user.id)
        .order_by(Invite.created_at.desc())
        .all()
    )
    return render_template(
        "account.html",
        invites=invites,
        mode=registration_mode(),
        invite_limit=INVITE_LIMIT,
        devices=db.session.query(PushSubscription)
            .filter_by(user_id=current_user.id).count(),
    )


@bp.post("/profile")
@login_required
def update_profile():
    name = (request.form.get("name") or "").strip()
    if len(name) > 120:
        flash("That name is too long.")
        return redirect(url_for("account.index"))
    current_user.name = name or None
    db.session.commit()
    flash("Profile saved.")
    return redirect(url_for("account.index"))


@bp.post("/invites")
@login_required
def create_invite():
    if registration_mode() != "invite":
        flash("Invites aren't in use on this server.")
        return redirect(url_for("account.index"))

    unused = (
        db.session.query(Invite)
        .filter_by(created_by_id=current_user.id, used_by_id=None)
        .count()
    )
    if unused >= INVITE_LIMIT:
        flash(f"You already have {INVITE_LIMIT} unused invites — share those first.")
        return redirect(url_for("account.index"))

    db.session.add(Invite(
        code=new_invite_code(),
        created_by_id=current_user.id,
        note=(request.form.get("note") or "").strip()[:120] or None,
    ))
    db.session.commit()
    flash("Invite created — share the link.")
    return redirect(url_for("account.index"))


@bp.post("/invites/<int:invite_id>/revoke")
@login_required
def revoke_invite(invite_id: int):
    invite = db.session.get(Invite, invite_id)
    if invite is None or invite.created_by_id != current_user.id:
        flash("That invite doesn't exist.")
    elif invite.is_used:
        flash("That invite has already been used — revoking it would not remove the account.")
    else:
        db.session.delete(invite)
        db.session.commit()
        flash("Invite revoked.")
    return redirect(url_for("account.index"))


@bp.get("/export")
@login_required
def export():
    """Everything this app knows about you, as one JSON file."""
    payload = build_export(current_user)
    body = json.dumps(payload, indent=2)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(
        body,
        mimetype="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="bunkmate-export-{stamp}.json"'
        },
    )


def build_export(user: User) -> dict:
    subjects = db.session.query(Subject).filter_by(user_id=user.id).all()
    subject_names = {s.id: s.code for s in subjects}
    semesters = db.session.query(Semester).filter_by(user_id=user.id).all()
    semester_ids = [s.id for s in semesters]

    lectures = db.session.query(LectureInstance).filter_by(user_id=user.id).all()
    lecture_ids = [l.id for l in lectures]
    changes = (
        db.session.query(LectureChange)
        .filter(LectureChange.lecture_id.in_(lecture_ids))
        .all()
        if lecture_ids else []
    )
    versions = (
        db.session.query(TimetableVersion)
        .filter(TimetableVersion.semester_id.in_(semester_ids))
        .all()
        if semester_ids else []
    )
    slots = (
        db.session.query(TimetableSlot)
        .filter(TimetableSlot.version_id.in_([v.id for v in versions]))
        .all()
        if versions else []
    )
    holidays = (
        db.session.query(Holiday)
        .filter(Holiday.semester_id.in_(semester_ids))
        .all()
        if semester_ids else []
    )
    settings = db.session.get(Settings, user.id)

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "profile": {
            "email": user.email,
            "name": user.name,
            "student_number": user.student_number,
            "roll_no": user.roll_no,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
        "settings": {
            "subject_limit": settings.subject_limit,
            "overall_limit": settings.overall_limit,
            "staleness_days": settings.staleness_days,
            "notify_enabled": settings.notify_enabled,
            "notify_hour": settings.notify_hour,
        } if settings else None,
        "semesters": [
            {
                "session": s.session_label,
                "start_date": s.start_date.isoformat() if s.start_date else None,
                "end_date": s.end_date.isoformat() if s.end_date else None,
            }
            for s in semesters
        ],
        "subjects": [
            {
                "code": s.code,
                "canonical_name": s.canonical_name,
                "lecture_type": s.lecture_type,
                "custom_limit": s.custom_limit,
                "active": s.active,
            }
            for s in subjects
        ],
        "aliases": [
            {"raw_name": a.raw_name, "subject": subject_names.get(a.subject_id),
             "source": a.source}
            for a in db.session.query(CourseAlias).filter_by(user_id=user.id).all()
        ],
        "lectures": [
            {
                "subject": subject_names.get(l.subject_id),
                "date": l.on_date.isoformat(),
                "start_time": l.start_time.isoformat(),
                "end_time": l.end_time.isoformat(),
                "status": l.status,
                "vanished": l.is_vanished,
            }
            for l in lectures
        ],
        "lecture_changes": [
            {
                "lecture_id": c.lecture_id,
                "from": c.from_status,
                "to": c.to_status,
                "at": c.changed_at.isoformat() if c.changed_at else None,
            }
            for c in changes
        ],
        "reports": [
            {
                "uploaded_at": r.uploaded_at.isoformat() if r.uploaded_at else None,
                "period_start": r.period_start.isoformat(),
                "period_end": r.period_end.isoformat(),
                "lecture_count": r.lecture_count,
                "status": r.status,
                "original_filename": r.original_filename,
                "sha256": r.file_sha256,
            }
            for r in db.session.query(ReportSnapshot).filter_by(user_id=user.id).all()
        ],
        "timetable": [
            {
                "weekday": slot.weekday,
                "start_time": slot.start_time.isoformat(),
                "end_time": slot.end_time.isoformat(),
                "subject": subject_names.get(slot.subject_id),
            }
            for slot in slots
        ],
        "calendar": [
            {"date": h.on_date.isoformat(), "kind": h.kind, "name": h.name,
             "swap_weekday": h.swap_weekday}
            for h in holidays
        ],
        "planned_absences": [
            {"date": p.on_date.isoformat(),
             "subject": subject_names.get(p.subject_id) if p.subject_id else None,
             "note": p.note}
            for p in db.session.query(PlannedAbsence).filter_by(user_id=user.id).all()
        ],
    }


@bp.post("/delete")
@login_required
def delete():
    """Erase the account. Password-confirmed, and it takes the PDFs too."""
    password = request.form.get("password") or ""
    if not current_user.check_password(password):
        flash("That password doesn't match — nothing was deleted.")
        return redirect(url_for("account.index"))

    user_id = current_user.id
    upload_dir = os.path.join(current_app.config["UPLOAD_DIR"], str(user_id))

    logout_user()
    purge_user(db.session.get(User, user_id))

    # Raw PDFs live outside the database and would otherwise survive deletion.
    shutil.rmtree(upload_dir, ignore_errors=True)

    flash("Your account and all of its data have been deleted.")
    return redirect(url_for("auth.login"))


def purge_user(user: User) -> None:
    """Delete every row belonging to `user`, children first.

    Explicit and ordered rather than relying on cascades: foreign keys are on
    now, so a wrong order fails loudly instead of orphaning a ledger.
    """
    semester_ids = [
        s.id for s in db.session.query(Semester).filter_by(user_id=user.id).all()
    ]
    lecture_ids = [
        l.id for l in db.session.query(LectureInstance).filter_by(user_id=user.id).all()
    ]
    version_ids = [
        v.id for v in db.session.query(TimetableVersion)
        .filter(TimetableVersion.semester_id.in_(semester_ids)).all()
    ] if semester_ids else []

    if lecture_ids:
        db.session.query(LectureChange).filter(
            LectureChange.lecture_id.in_(lecture_ids)
        ).delete(synchronize_session=False)
    if version_ids:
        db.session.query(TimetableSlot).filter(
            TimetableSlot.version_id.in_(version_ids)
        ).delete(synchronize_session=False)
    if semester_ids:
        db.session.query(TimetableVersion).filter(
            TimetableVersion.semester_id.in_(semester_ids)
        ).delete(synchronize_session=False)
        db.session.query(Holiday).filter(
            Holiday.semester_id.in_(semester_ids)
        ).delete(synchronize_session=False)

    for model in (LectureInstance, CourseAlias, PlannedAbsence,
                  ReportSnapshot, PushSubscription):
        db.session.query(model).filter_by(user_id=user.id).delete(
            synchronize_session=False
        )

    db.session.query(Subject).filter_by(user_id=user.id).delete(
        synchronize_session=False
    )
    db.session.query(Semester).filter_by(user_id=user.id).delete(
        synchronize_session=False
    )
    db.session.query(Settings).filter_by(user_id=user.id).delete(
        synchronize_session=False
    )

    # Invites this person sent stay valid for whoever holds them, but must stop
    # pointing at a row that no longer exists.
    db.session.query(Invite).filter_by(created_by_id=user.id).delete(
        synchronize_session=False
    )
    db.session.query(Invite).filter_by(used_by_id=user.id).update(
        {"used_by_id": None}, synchronize_session=False
    )

    db.session.delete(user)
    db.session.commit()
