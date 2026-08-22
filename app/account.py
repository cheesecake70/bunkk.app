"""Account actions — profile edits and deletion.

These live behind Settings rather than a page of their own: there was never
enough on an Account tab to justify making people choose between two places to
look. Deletion still belongs here, and still takes the raw PDFs with it (§6.4).
"""
from __future__ import annotations

import os
import shutil

from flask import Blueprint, current_app, flash, redirect, request, url_for
from flask_login import current_user, login_required, logout_user

from . import db
from .auth import normalise_username, validate_username
from .models import (
    CourseAlias,
    Holiday,
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


@bp.get("/")
@login_required
def index():
    # Account and Settings are one page now; keep the old address working.
    return redirect(url_for("core.settings"))


@bp.post("/profile")
@login_required
def update_profile():
    username = normalise_username(request.form.get("username"))
    name = (request.form.get("name") or "").strip()

    if username and username.lower() != (current_user.username or "").lower():
        error = validate_username(username)
        if error:
            flash(error)
            return redirect(url_for("core.settings"))
        current_user.username = username

    if len(name) > 120:
        flash("That name is too long.")
        return redirect(url_for("core.settings"))

    current_user.name = name or None
    db.session.commit()
    flash("Profile saved.")
    return redirect(url_for("core.settings"))


@bp.post("/delete")
@login_required
def delete():
    """Erase the account. Password-confirmed, and it takes the PDFs too."""
    password = request.form.get("password") or ""
    if not current_user.check_password(password):
        flash("That password doesn't match — nothing was deleted.")
        return redirect(url_for("core.settings"))

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

    db.session.delete(user)
    db.session.commit()
