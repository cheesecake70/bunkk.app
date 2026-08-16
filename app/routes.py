"""Server-rendered pages (ADR-1: Jinja + plain CSS, no build step)."""
from __future__ import annotations

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from . import db
from .models import Subject
from .services import (
    changes_for_lectures,
    coverage_for,
    dashboard_for,
    lectures_for_subject,
    settings_for,
    snapshots_for,
    subject_limit,
    vanished_lectures,
)

bp = Blueprint("core", __name__)


@bp.get("/healthz")
def healthz():
    return jsonify(status="ok", app="bunkmate", phase=1)


@bp.get("/")
@login_required
def dashboard():
    dash = dashboard_for(current_user)
    return render_template(
        "dashboard.html",
        dash=dash,
        coverage=coverage_for(current_user),
        vanished=vanished_lectures(current_user),
        has_data=bool(dash.subjects),
    )


@bp.get("/upload")
@login_required
def upload():
    return render_template("upload.html", coverage=coverage_for(current_user))


@bp.get("/subjects/<int:subject_id>")
@login_required
def subject_detail(subject_id: int):
    subject = db.session.get(Subject, subject_id)
    if subject is None or subject.user_id != current_user.id:
        abort(404)

    dash = dashboard_for(current_user)
    stats = next((s for s in dash.subjects if s.subject_id == subject_id), None)
    lectures = lectures_for_subject(current_user, subject_id)

    return render_template(
        "subject.html",
        subject=subject,
        stats=stats,
        overall=dash.overall,
        lectures=lectures,
        history=changes_for_lectures([l.id for l in lectures]),
    )


@bp.get("/history")
@login_required
def history():
    return render_template(
        "history.html",
        snapshots=snapshots_for(current_user),
        coverage=coverage_for(current_user),
    )


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    settings = settings_for(current_user)
    subjects = (
        db.session.query(Subject)
        .filter_by(user_id=current_user.id)
        .order_by(Subject.code)
        .all()
    )

    if request.method == "POST":
        errors = _apply_settings(settings, subjects, request.form)
        if errors:
            db.session.rollback()
            return render_template(
                "settings.html", settings=settings, subjects=subjects,
                errors=errors, subject_limit=subject_limit,
            ), 400
        db.session.commit()
        flash("Settings saved.")
        return redirect(url_for("core.settings"))

    return render_template(
        "settings.html", settings=settings, subjects=subjects,
        errors={}, subject_limit=subject_limit,
    )


def _apply_settings(settings, subjects, form) -> dict[str, str]:
    errors: dict[str, str] = {}

    def percent(field: str, label: str) -> int | None:
        raw = (form.get(field) or "").strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            errors[field] = f"{label} must be a whole number."
            return None
        if not 0 <= value <= 100:
            errors[field] = "Must be between 0 and 100."
            return None
        return value

    overall = percent("overall_limit", "Overall limit")
    subject = percent("subject_limit", "Subject limit")
    if overall is not None:
        settings.overall_limit = overall
    if subject is not None:
        settings.subject_limit = subject

    try:
        days = int((form.get("staleness_days") or settings.staleness_days))
        if days < 1:
            raise ValueError
        settings.staleness_days = days
    except ValueError:
        errors["staleness_days"] = "Enter a number of days (1 or more)."

    for s in subjects:
        value = percent(f"custom_limit_{s.id}", f"{s.code} limit")
        raw = (form.get(f"custom_limit_{s.id}") or "").strip()
        s.custom_limit = value if raw else None

    return errors
