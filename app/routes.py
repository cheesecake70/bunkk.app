"""Server-rendered pages (ADR-1: Jinja + plain CSS, no build step)."""
from __future__ import annotations

import os
from calendar import monthrange
from datetime import date, time, timedelta

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

from . import db, planning, push
from .models import PlannedAbsence, PushSubscription, Subject
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
    return jsonify(status="ok", app="bunkr", phase=3)


@bp.get("/sw.js")
def service_worker():
    """Served from the root so the worker's scope covers every page.

    From /static/ it could only control /static/, which would make navigation
    handling and push impossible.
    """
    response = send_from_directory(
        os.path.join(current_app.root_path, "..", "static", "js"), "sw.js"
    )
    response.headers["Content-Type"] = "application/javascript"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = "/"
    return response


@bp.get("/offline")
def offline():
    """Shown by the service worker when a navigation can't reach the server.

    Deliberately carries no attendance data: stale numbers are worse than none.
    """
    return render_template("offline.html")


@bp.get("/")
@login_required
def dashboard():
    dash = dashboard_for(current_user)
    ready, next_step = planning.advanced_ready(current_user)

    wallet = strip = None
    if ready:
        wallet = planning.wallet_for(current_user)
        strip = planning.day_strip(current_user, wallet=wallet)

    return render_template(
        "dashboard.html",
        dash=dash,
        coverage=coverage_for(current_user),
        vanished=vanished_lectures(current_user),
        has_data=bool(dash.subjects),
        advanced_ready=ready,
        next_step=next_step,
        wallet=wallet,
        strip=strip,
        windows=planning.windows(current_user) if dash.subjects else None,
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


@bp.route("/timetable", methods=["GET", "POST"])
@login_required
def timetable():
    """Confirm the grid Bunkr inferred, rather than typing one in."""
    subjects = {
        s.id: s
        for s in db.session.query(Subject).filter_by(user_id=current_user.id).all()
    }

    if request.method == "POST":
        chosen = request.form.getlist("slot")
        slots = []
        for token in chosen:
            try:
                subject_id, weekday, start, end = token.split("|")
                slots.append(planning.Slot(
                    subject_id=int(subject_id),
                    weekday=int(weekday),
                    start_time=time.fromisoformat(start),
                    end_time=time.fromisoformat(end),
                ))
            except (ValueError, TypeError):
                continue
        if not slots:
            flash("Pick at least one slot — an empty timetable can't project anything.")
            return redirect(url_for("core.timetable"))

        planning.save_timetable(current_user, slots, source="inferred")
        flash(f"Timetable saved — {len(slots)} weekly slots.")
        return redirect(url_for("core.calendar"))

    candidates = planning.inferred_candidates(current_user)
    active = {
        (s.subject_id, s.weekday, s.start_time) for s in planning.active_slots(current_user)
    }
    return render_template(
        "timetable.html",
        candidates=candidates,
        subjects=subjects,
        active=active,
        has_existing=bool(active),
        weekdays=WEEKDAYS,
    )


@bp.get("/calendar")
@login_required
def calendar():
    end = planning.semester_end(current_user)
    today = date.today()
    holidays = {h.on_date: h for h in planning.holidays_for(current_user)}

    months = _month_grid(today, end) if end else _month_grid(today, today + timedelta(days=60))
    return render_template(
        "calendar.html",
        months=months,
        holidays=holidays,
        semester_end=end,
        today=today,
        weekdays=WEEKDAYS,
        has_timetable=planning.has_timetable(current_user),
    )


@bp.post("/calendar/end-date")
@login_required
def set_end_date():
    raw = (request.form.get("end_date") or "").strip()
    try:
        end = date.fromisoformat(raw)
    except ValueError:
        flash("Enter the semester end date as YYYY-MM-DD.")
        return redirect(url_for("core.calendar"))
    if end <= date.today():
        flash("The semester end date needs to be in the future.")
        return redirect(url_for("core.calendar"))

    planning.set_semester_end(current_user, end)
    flash(f"Semester ends {end:%d %b %Y} — projections are live.")
    return redirect(url_for("core.dashboard"))


@bp.get("/plan")
@login_required
def plan():
    ready, next_step = planning.advanced_ready(current_user)
    if not ready:
        return render_template("plan.html", ready=False, next_step=next_step)

    wallet = planning.wallet_for(current_user)
    absences = (
        db.session.query(PlannedAbsence)
        .filter_by(user_id=current_user.id)
        .order_by(PlannedAbsence.on_date)
        .all()
    )
    subjects = {
        s.id: s
        for s in db.session.query(Subject).filter_by(user_id=current_user.id).all()
    }
    return render_template(
        "plan.html",
        ready=True,
        wallet=wallet,
        strip=planning.day_strip(current_user, wallet=wallet),
        absences=absences,
        subjects=subjects,
        result=planning.simulate_for(current_user, []),
    )


def _month_grid(start: date, end: date) -> list[dict]:
    """Whole months from `start`'s month to `end`'s, as week rows of dates.

    Days outside [start, end] are rendered as blanks — the calendar is
    future-only, so there is never a reason to tap a day that already happened.
    """
    months = []
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)

    while cursor <= last:
        _, days_in_month = monthrange(cursor.year, cursor.month)
        first_weekday = cursor.weekday()
        cells: list[date | None] = [None] * first_weekday
        cells += [date(cursor.year, cursor.month, d) for d in range(1, days_in_month + 1)]
        while len(cells) % 7:
            cells.append(None)

        months.append({
            "label": cursor.strftime("%B %Y"),
            "weeks": [cells[i:i + 7] for i in range(0, len(cells), 7)],
        })
        cursor = date(cursor.year + (cursor.month == 12),
                      cursor.month % 12 + 1, 1)
    return months


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

    def page(errors, status=200):
        return render_template(
            "settings.html", settings=settings, subjects=subjects,
            errors=errors, subject_limit=subject_limit,
            push_configured=push.is_configured(),
            push_public_key=push.public_key(),
            push_devices=db.session.query(PushSubscription)
                .filter_by(user_id=current_user.id).count(),
        ), status

    if request.method == "POST":
        # The notification card posts on its own so saving a time doesn't
        # require re-submitting every limit on the page.
        if request.form.get("only_notify_hour"):
            errors = _apply_notify_hour(settings, request.form)
            if errors:
                db.session.rollback()
                return page(errors, 400)
            db.session.commit()
            flash("Notification time saved.")
            return redirect(url_for("core.settings"))

        errors = _apply_settings(settings, subjects, request.form)
        if errors:
            db.session.rollback()
            return page(errors, 400)
        db.session.commit()
        flash("Settings saved.")
        return redirect(url_for("core.settings"))

    return page({})


def _apply_notify_hour(settings, form) -> dict[str, str]:
    raw = (form.get("notify_hour") or "").strip()
    try:
        hour = int(raw)
    except ValueError:
        return {"notify_hour": "Enter an hour between 0 and 23."}
    if not 0 <= hour <= 23:
        return {"notify_hour": "Enter an hour between 0 and 23."}
    settings.notify_hour = hour
    return {}


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
