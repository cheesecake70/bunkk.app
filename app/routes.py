"""Server-rendered pages (ADR-1: Jinja + plain CSS, no build step)."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, time, timedelta

from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

from . import db, planning
from .models import PlannedAbsence, Subject
from .services import (
    UNKNOWN_STATUSES,
    apply_subject_edits,
    changes_for_lectures,
    coverage_for,
    dashboard_for,
    lectures_for_subject,
    predictions_for,
    settings_for,
    subject_limit,
    vanished_lectures,
)

bp = Blueprint("core", __name__)


@bp.get("/healthz")
def healthz():
    return jsonify(status="ok", app="bunkr")


def _week_from(day: date) -> list[date]:
    """The seven days Today's arrows can reach, starting with `day`.

    Deliberately a rolling window rather than Monday-to-Sunday: on a Sunday a
    calendar week has no days left in it, and "can I skip tomorrow?" is exactly
    the question someone asks on a Sunday evening. Past days carry no decision
    anyway — the college has already recorded them.
    """
    return [day + timedelta(days=i) for i in range(7)]


@bp.get("/")
def dashboard():
    """Today: one question — can I skip today?

    Everything semester-wide lives on /plan. What is left here is the day in
    front of you, and the arrows to walk the rest of this week. Signed out,
    this address is the public landing page instead.
    """
    if not current_user.is_authenticated:
        return render_template("landing.html")
    dash = dashboard_for(current_user)
    ready, next_step = planning.advanced_ready(current_user)
    today = date.today()

    week = _week_from(today)
    focus = today
    raw = (request.args.get("date") or "").strip()
    if raw:
        try:
            asked = date.fromisoformat(raw)
        except ValueError:
            asked = today
        # Confined to the visible week on purpose: further out is a planning
        # question, and /plan answers that across the whole horizon.
        focus = asked if asked in week else today

    wallet = day = None
    whole_day = None
    planned = {}
    if ready:
        wallet = planning.wallet_for(current_user)
        day = planning.day_plan_on(current_user, focus, wallet=wallet)
        # Without this the skip buttons render as "Skip this" on every load,
        # however many absences are already committed for the day.
        whole_day, planned = planning.absences_on(current_user, focus)

    windows = planning.windows(current_user) if dash.subjects else None
    return render_template(
        "today.html",
        dash=dash,
        has_data=bool(dash.subjects),
        advanced_ready=ready,
        next_step=next_step,
        wallet=wallet,
        day=day,
        focus=focus,
        today=today,
        week=week,
        windows=windows,
        whole_day=whole_day,
        planned=planned,
        subjects_by_id=(wallet.by_id() if wallet else {}),
    )


@bp.get("/overview")
@login_required
def overview():
    """Folded into /plan — the two pages asked the same question twice."""
    return redirect(url_for("core.plan"), code=301)


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

    predictions = predictions_for(current_user)
    # Gates the skip ladder: without a timetable and term dates there are no
    # future lectures to skip, and the endpoint would answer with an empty one.
    advanced_ready, _ = planning.advanced_ready(current_user)
    return render_template(
        "subject.html",
        subject=subject,
        stats=stats,
        advanced_ready=advanced_ready,
        overall=dash.overall,
        lectures=lectures,
        history=changes_for_lectures([l.id for l in lectures]),
        predictions=predictions,
        unknown_statuses=UNKNOWN_STATUSES,
        # What is left to decide, not the raw NU count — the guessed ones have
        # already moved into the figures above, so counting them here would
        # contradict the meter three lines up.
        pending_count=sum(
            1 for l in lectures
            if l.status in UNKNOWN_STATUSES
            and not l.is_vanished
            and l.id not in predictions
        ),
    )


@bp.route("/timetable", methods=["GET", "POST"])
@login_required
def timetable():
    """The weekly grid: seeded from what the reports imply, editable after.

    Inference is a good first draft — a lecture recurring at the same weekday
    and time across several weeks really is a weekly slot — but it can only
    ever describe what has already happened. Anything the reports haven't seen
    yet, and every break between classes, has to be addable by hand.
    """
    subjects = (
        db.session.query(Subject)
        .filter_by(user_id=current_user.id)
        .order_by(Subject.code)
        .all()
    )

    if request.method == "POST":
        entries = _parse_grid(request.form)
        if not any(e.kind == "class" for e in entries):
            flash("Add at least one class — an empty timetable can't project anything.",
                  "error")
            return redirect(url_for("core.timetable"))

        planning.save_timetable(current_user, entries, source="manual")
        classes = sum(1 for e in entries if e.kind == "class")
        flash(f"Timetable updated — {classes} classes a week.")
        return redirect(url_for("core.timetable"))

    entries = planning.timetable_entries(current_user)
    if not entries:
        # First visit: offer the inferred grid as a starting point rather than
        # an empty week. Nothing is saved until the user presses save.
        drafted = [
            planning.Entry(c.slot.weekday, c.slot.start_time, c.slot.end_time,
                           kind="class", subject_id=c.slot.subject_id)
            for c in planning.inferred_candidates(current_user) if c.is_confident
        ]
        inferred = bool(drafted)
        # The gaps between inferred classes are free periods whether or not the
        # reports named them, and drawing them makes "leave before lunch"
        # mean something the first time the grid is seen.
        entries = planning.fill_gaps(drafted)
    else:
        inferred = False

    return render_template(
        "timetable.html",
        entries=entries,
        subjects=subjects,
        subjects_by_id={s.id: s for s in subjects},
        weekdays=WEEKDAYS,
        inferred=inferred,
    )


def _parse_grid(form) -> list[planning.Entry]:
    """Rebuild the grid from the form's parallel arrays.

    Every block posts one value into each of six same-length lists, so a row is
    the i-th value of each. Validation lives in `planning.entries_from`, shared
    with the JSON endpoint the editor autosaves through.
    """
    kinds = form.getlist("kind")
    weekdays = form.getlist("weekday")
    starts = form.getlist("start")
    ends = form.getlist("end")
    subject_ids = form.getlist("subject_id")
    labels = form.getlist("label")

    def at(values, i):
        return values[i] if i < len(values) else None

    return planning.entries_from([
        {
            "kind": at(kinds, i),
            "weekday": at(weekdays, i),
            "start": at(starts, i),
            "end": at(ends, i),
            "subject_id": at(subject_ids, i),
            "label": at(labels, i),
        }
        for i in range(len(kinds))
    ])
@bp.get("/calendar")
@login_required
def calendar():
    end = planning.semester_end(current_user)
    today = date.today()
    holidays = {h.on_date: h for h in planning.holidays_for(current_user)}

    months = _month_grid(today, end) if end else _month_grid(today, today + timedelta(days=60))

    # Absences per day, so the whole semester's commitments are visible at a
    # glance rather than one dialog at a time.
    absence_counts: dict[date, int] = {}
    for row in db.session.query(PlannedAbsence).filter_by(user_id=current_user.id).all():
        absence_counts[row.on_date] = absence_counts.get(row.on_date, 0) + 1

    win = planning.windows(current_user)
    return render_template(
        "calendar.html",
        months=months,
        holidays=holidays,
        absence_counts=absence_counts,
        checkpoints={c.on_date: c for c in planning.checkpoints_for(current_user)},
        horizon_to=win.remaining_to,
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
        flash("Enter the semester end date as YYYY-MM-DD.", "error")
        return redirect(url_for("core.calendar"))
    if end <= date.today():
        flash("The semester end date needs to be in the future.", "error")
        return redirect(url_for("core.calendar"))

    planning.set_semester_end(current_user, end)
    flash(f"Semester ends {end:%d %b %Y} — projections are live.")
    return redirect(url_for("core.dashboard"))


@bp.get("/checkpoints")
@login_required
def checkpoints():
    return _checkpoints_page()


def _checkpoints_page(error: str | None = None, status: int = 200):
    """Shared by GET and the POST error path, so a rejected date re-renders
    the list instead of bouncing you to an empty form."""
    today = date.today()
    rows = planning.checkpoints_for(current_user)
    upcoming = planning.next_checkpoint(current_user, today)
    return render_template(
        "checkpoints.html",
        checkpoints=rows,
        next_checkpoint=upcoming,
        semester_end=planning.semester_end(current_user),
        today=today,
        error=error,
    ), status


@bp.post("/checkpoints")
@login_required
def add_checkpoint():
    raw = (request.form.get("on_date") or "").strip()
    try:
        on_date = date.fromisoformat(raw)
    except ValueError:
        return _checkpoints_page("Enter the date as YYYY-MM-DD.", 400)

    error = planning.add_checkpoint(
        current_user, on_date, (request.form.get("label") or "").strip()
    )
    if error:
        return _checkpoints_page(error, 400)

    flash(f"Checkpoint added for {on_date:%d %b %Y}.")
    return redirect(url_for("core.checkpoints"))


@bp.post("/checkpoints/<int:checkpoint_id>/delete")
@login_required
def delete_checkpoint(checkpoint_id: int):
    if not planning.remove_checkpoint(current_user, checkpoint_id):
        abort(404)
    flash("Checkpoint removed.")
    return redirect(url_for("core.checkpoints"))


def _subject_rows(dash, wallet) -> list[dict]:
    """One row per subject, ledger figures and planning figures side by side.

    Zipped here rather than in the template so the page holds no lookup logic.
    Driven from the ledger, because `Dashboard` covers every subject that has
    ever appeared in a report while `Wallet` only covers the ones the timetable
    knows about — a subject with no slot must still show its attendance, just
    without the forward-looking columns.
    """
    plans = wallet.by_id() if wallet else {}
    return [{"stats": s, "plan": plans.get(s.subject_id)} for s in dash.subjects]


@bp.get("/plan")
@login_required
def plan():
    """The whole semester: where each subject stands, and what to spend."""
    dash = dashboard_for(current_user)
    ready, next_step = planning.advanced_ready(current_user)

    # Guesses feed the headline figures, so the untouched worst case has to stay
    # visible somewhere — otherwise a wrong guess is invisible as well as wrong.
    guesses = predictions_for(current_user)
    true_worst = dashboard_for(current_user, use_predictions=False) if guesses else None

    shell = {
        "dash": dash,
        "has_data": bool(dash.subjects),
        "guess_count": len(guesses),
        "true_worst": true_worst,
        "coverage": coverage_for(current_user),
        "vanished": vanished_lectures(current_user),
        "windows": planning.windows(current_user) if dash.subjects else None,
    }

    if not ready:
        # Still show the ledger — a missing timetable costs you the planning
        # half of the page, not your attendance.
        return render_template(
            "plan.html", ready=False, next_step=next_step, wallet=None,
            rows=_subject_rows(dash, None), **shell
        )

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
    win = planning.windows(current_user)
    strip = planning.horizon_strip(current_user, wallet=wallet)
    # The strip shows how much of each day is already spoken for; the day sheet
    # owns the detail, so all it needs here is a count.
    planned_counts = planning.planned_count_by_date(current_user, absences)
    return render_template(
        "plan.html",
        ready=True,
        wallet=wallet,
        rows=_subject_rows(dash, wallet),
        strip=strip,
        months=_group_by_month(strip),
        planned_counts=planned_counts,
        absences=absences,
        subjects=subjects,
        result=planning.simulate_for(current_user, []),
        **{**shell, "windows": win},
    )


def _group_by_month(strip) -> list[dict]:
    """A horizon can span months now, so the strip needs headings."""
    months: list[dict] = []
    for day in strip:
        label = day.on_date.strftime("%B %Y")
        if not months or months[-1]["label"] != label:
            months.append({"label": label, "days": []})
        months[-1]["days"].append(day)
    return months


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
        ), status

    if request.method == "POST":
        errors = _apply_settings(settings, subjects, request.form)
        if errors:
            db.session.rollback()
            return page(errors, 400)
        db.session.commit()
        flash("Settings saved.")
        return redirect(url_for("core.settings"))

    return page({})


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

    # `staleness_days` is no longer a setting anyone turns — it keeps its default
    # and still drives the "your report is N days old" banners.
    return errors


@bp.route("/subjects", methods=["GET", "POST"])
@login_required
def subjects():
    """Rename subjects and set per-subject limits.

    Renaming is display-only: `CourseAlias` keeps mapping the portal's printed
    names to these rows, so calling something "DBMS" here can never stop a
    future report merging into it.
    """
    rows = (
        db.session.query(Subject)
        .filter_by(user_id=current_user.id)
        .order_by(Subject.code)
        .all()
    )

    def page(errors, status=200):
        return render_template(
            "subjects.html", subjects=rows, errors=errors,
            settings=settings_for(current_user), subject_limit=subject_limit,
        ), status

    if request.method == "POST":
        errors = _apply_subjects(rows, request.form)
        if errors:
            db.session.rollback()
            return page(errors, 400)
        db.session.commit()
        flash("Subjects updated.")
        return redirect(url_for("core.subjects"))

    return page({})


def _apply_subjects(subjects, form) -> dict[str, str]:
    errors: dict[str, str] = {}

    for s in subjects:
        # A subject the form never mentions is left alone. Otherwise a partial
        # post — or a future page that edits one row — would read every absent
        # field as "cleared" and reject the lot.
        values = {
            field: form[f"{field}_{s.id}"]
            for field in ("name", "code", "custom_limit")
            if f"{field}_{s.id}" in form
        }
        if not values:
            continue

        for field, message in apply_subject_edits(s, subjects, values).items():
            errors[f"{field}_{s.id}"] = message

    return errors
