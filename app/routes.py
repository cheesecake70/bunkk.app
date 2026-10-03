"""Server-rendered pages (ADR-1: Jinja + plain CSS, no build step)."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from markupsafe import Markup
from flask_login import current_user, login_required
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from . import db, planning
from .filters import WEEKDAYS
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
    subjects_for,
    vanished_lectures,
)

bp = Blueprint("core", __name__)


@bp.get("/healthz")
def healthz():
    """Liveness for the reverse proxy or an uptime monitor.

    Asks the database a question too: a process that is up but can't reach
    its ledger is not healthy, and "ok" would keep traffic flowing to it.
    """
    try:
        db.session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        current_app.logger.exception("healthz: database unreachable")
        return jsonify(status="error", app="bunkk"), 503
    return jsonify(status="ok", app="bunkk")


def _week_around(focus: date, today: date) -> list[date]:
    """The seven days on the strip, sliding so `focus` is always one of them.

    Deliberately a rolling window rather than Monday-to-Sunday: on a Sunday a
    calendar week has no days left in it, and "can I skip tomorrow?" is exactly
    the question someone asks on a Sunday evening.

    It used to be pinned to today, which turned the seventh day into a wall —
    the arrow greyed out with nothing to say about why, and the answer to "what
    about the week after" was to go and find it on another page. Walking forward
    now slides the window a day at a time. It still never shows the past: those
    days carry no decision, the college has already recorded them.
    """
    start = max(today, focus - timedelta(days=6))
    return [start + timedelta(days=i) for i in range(7)]


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

    # How far forward the arrows go. With a semester end you can walk to it;
    # without one there is no projection to walk through, so the strip stays
    # the week it always was.
    end = planning.semester_end(current_user)
    horizon = end or today + timedelta(days=6)

    focus = today
    raw = (request.args.get("date") or "").strip()
    if raw:
        try:
            asked = date.fromisoformat(raw)
        except ValueError:
            asked = today
        # Clamped rather than discarded: a hand-typed date outside the term
        # should land you at the nearest day that exists, not silently back on
        # today as though the request were nonsense.
        focus = min(max(asked, today), horizon)

    week = _week_around(focus, today)
    # Named rather than derived from the strip: walking forward puts `focus` at
    # the right-hand edge, so "the next day" is off the end of the list.
    prev_day = focus - timedelta(days=1) if focus > today else None
    next_day = focus + timedelta(days=1) if focus < horizon else None

    wallet = day = None
    whole_day = None
    planned = {}
    result = None
    next_up = None
    if ready:
        wallet = planning.wallet_for(current_user)
        day = planning.day_plan_on(current_user, focus, wallet=wallet)
        # Without this the skip buttons render as "Skip this" on every load,
        # however many absences are already committed for the day.
        whole_day, planned = planning.absences_on(current_user, focus)
        # What the plan already breaks. The commit guard warns about what a new
        # absence would break *newly* — a limit you blew last week is not news,
        # and a dialog that fires every time stops being read.
        result = planning.simulate_for(current_user, [], wallet=wallet)
        # A day with no classes answers "can I skip?" with nothing at all, and a
        # Saturday shouldn't be a dead end. Point at the next day that has an
        # answer instead.
        if day is not None and not day.lectures:
            next_up = _next_teaching_day(current_user, focus, horizon, wallet)

    holiday = next(
        (h for h in planning.holidays_for(current_user) if h.on_date == focus), None
    )
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
        prev_day=prev_day,
        next_day=next_day,
        result=result,
        holiday=holiday,
        next_up=next_up,
        # The report going stale is the single most common reason a number here
        # looks wrong, so Today says so rather than leaving it to /plan.
        coverage=coverage_for(current_user) if dash.subjects else None,
        subjects_by_id=(wallet.by_id() if wallet else {}),
    )


def _next_teaching_day(user, focus: date, horizon: date, wallet):
    """The next day with classes on it, within a week of `focus`.

    Walks forward from the focused day rather than through the visible strip:
    once the strip slides, `focus` sits at its right-hand edge and there is
    nothing after it to look at.
    """
    for offset in range(1, 8):
        day = focus + timedelta(days=offset)
        if day > horizon:
            return None
        plan = planning.day_plan_on(user, day, wallet=wallet)
        if plan is not None and plan.lectures:
            return plan
    return None


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
        history=changes_for_lectures(current_user, [l.id for l in lectures]),
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
    subjects = subjects_for(current_user)

    if request.method == "POST":
        # Filtered to what would actually be stored *before* the guard: a grid
        # whose every class named someone else's subject would otherwise pass
        # here and then land empty.
        entries = planning.storable_entries(current_user, _parse_grid(request.form))
        if not any(e.kind == "class" for e in entries):
            flash("Add at least one class — an empty timetable can't project anything.",
                  "error")
            return redirect(url_for("core.timetable"))

        planning.save_timetable(current_user, entries, source="manual")
        classes = sum(1 for e in entries if e.kind == "class")
        # A grid on its own projects nothing; without the end date this page
        # looked finished and the verdicts stayed off with no explanation.
        if planning.semester_end(current_user) is None:
            flash(Markup(
                f"Timetable updated — {classes} classes a week. One step left: "
                f'<a href="{url_for("core.calendar")}">set your semester end date</a>.'
            ))
        else:
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
        overlaps=planning.overlapping(entries),
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
    # glance rather than one dialog at a time. Counted in *lectures*, through
    # the timetable — counting rows made a whole-day plan read as 1 against a
    # seven-lecture day, disagreeing with the day sheet, the plan strip, and
    # with itself the moment the sheet repainted the same badge.
    absence_counts = planning.planned_count_by_date(
        current_user,
        db.session.query(PlannedAbsence).filter_by(user_id=current_user.id).all(),
    )

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
        # Seeds the commit guard, exactly as on Today and Plan.
        result=(planning.simulate_for(current_user, [])
                if planning.advanced_ready(current_user)[0] else None),
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
    if end > date.today() + timedelta(days=planning.MAX_SEMESTER_DAYS):
        flash("That's more than a year away — check the year.", "error")
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

    win = planning.windows(current_user) if dash.subjects else None
    shell = {
        "dash": dash,
        "has_data": bool(dash.subjects),
        "guess_count": len(guesses),
        "true_worst": true_worst,
        "coverage": coverage_for(current_user),
        "vanished": vanished_lectures(current_user),
        "windows": win,
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
    subjects = {s.id: s for s in subjects_for(current_user)}
    strip = planning.horizon_strip(current_user, wallet=wallet)
    # The strip shows how much of each day is already spoken for; the day sheet
    # owns the detail, so all it needs here is a count.
    planned_counts = planning.planned_count_by_date(current_user, absences)

    # A term is seventy-odd teaching days, and as a row of chips that was a wall
    # of identical cells you had to scroll past to reach anything else. The same
    # information reads at a glance as a month grid — and weekends only earn a
    # column when the timetable actually uses them.
    weekdays_shown = _weekdays_in_use(current_user)
    return render_template(
        "plan.html",
        ready=True,
        wallet=wallet,
        rows=_subject_rows(dash, wallet),
        strip=strip,
        plans={d.on_date: d for d in strip},
        months=(_month_grid(strip[0].on_date, strip[-1].on_date, weekdays_shown)
                if strip else []),
        weekdays_shown=weekdays_shown,
        planned_counts=planned_counts,
        absences=absences,
        subjects=subjects,
        # The wallet above is the same one a fresh simulation would build.
        result=planning.simulate_for(current_user, [], wallet=wallet),
        **shell,
    )


def _weekdays_in_use(user) -> tuple[int, ...]:
    """Mon–Fri unless the timetable puts something on a weekend."""
    used = {s.weekday for s in planning.active_slots(user)}
    return tuple(range(7)) if used - set(range(5)) else tuple(range(5))


def _month_grid(start: date, end: date,
                weekdays: tuple[int, ...] = tuple(range(7))) -> list[dict]:
    """Whole months from `start`'s month to `end`'s, as week rows of dates.

    Days outside [start, end] are rendered as blanks — the calendar is
    future-only, so there is never a reason to tap a day that already happened.

    `weekdays` narrows the columns: a Monday-to-Friday timetable has nothing to
    say about Saturdays, and giving them a column each costs two sevenths of the
    grid to say "no class" seventy times.
    """
    months = []
    cursor = date(start.year, start.month, 1)
    last = date(end.year, end.month, 1)
    width = len(weekdays)

    while cursor <= last:
        _, days_in_month = monthrange(cursor.year, cursor.month)
        days = [date(cursor.year, cursor.month, d)
                for d in range(1, days_in_month + 1)
                if date(cursor.year, cursor.month, d).weekday() in weekdays]

        cells: list[date | None] = []
        if days:
            # Pad to the first shown day's column, so dates line up under their
            # weekday heading rather than starting flush left.
            cells += [None] * weekdays.index(days[0].weekday())
            cells += days
            while len(cells) % width:
                cells.append(None)

        months.append({
            "label": cursor.strftime("%B %Y"),
            "weeks": [cells[i:i + width] for i in range(0, len(cells), width)],
        })
        cursor = date(cursor.year + (cursor.month == 12),
                      cursor.month % 12 + 1, 1)
    return months


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    settings = settings_for(current_user)
    subjects = subjects_for(current_user)

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
    rows = subjects_for(current_user)

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
