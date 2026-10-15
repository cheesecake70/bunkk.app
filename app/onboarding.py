"""First-run setup, and the gate that insists on it.

Uploading a report shows percentages, and for most people that was the whole
app: the skip verdicts, the plan and the per-subject budgets all stay off
until Bunkk knows the limits that apply and when the semester ends — and
nobody went looking for the pages that ask. So the questions come to them,
three short screens long:

    upload — the report
    rules  — the two limits and the semester end, on one screen
    exams  — attendance audits before exams (optional)

`next_required` decides which of the first two is still owed; a timetable the
reports were too thin to detect adds one screen of its own. Accounts that
existed before any of this land in the same flow on their next visit.
"""
from __future__ import annotations

from datetime import date

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from . import db, planning, tracking
from .models import utcnow
from .services import apply_limits, settings_for

bp = Blueprint("setup", __name__)

#: Pages a half-set-up account can still reach. Uploading is a step of setup,
#: the timetable editor is its fallback, and nobody should be kept from their
#: own account settings — including the button that deletes it.
UNGATED = frozenset({"core.healthz", "core.upload", "core.timetable", "core.settings"})

#: The screens, in order, as the progress rail names them.
STEPS = (("upload", "Report"), ("rules", "Rules"), ("exams", "Exams"))


def next_required(user) -> str | None:
    """The first screen `user` still owes, or None once Bunkk can plan."""
    if planning.active_semester(user) is None:
        return "upload"
    if user.setup_done_at is None or planning.semester_end(user) is None:
        return "rules"
    if not planning.has_timetable(user):
        # Uploads detect the timetable themselves; this catches accounts whose
        # reports arrived before they did.
        planning.adopt_detected_timetable(user)
        if not planning.has_timetable(user):
            return "timetable"
    return None


def mark_limits_chosen(user) -> None:
    """Record that the limits on file are ones this person picked."""
    if user.setup_done_at is None:
        user.setup_done_at = utcnow()
        tracking.record(user, "limits_chosen")


@bp.before_app_request
def gate():
    """Send a signed-in page view to the welcome flow until setup is done.

    Page views only: API calls come from pages that were already let through,
    and refusing a form post would throw away what was typed into it.
    """
    if not current_app.config.get("SETUP_GATE", True):
        return None
    if request.method != "GET" or request.blueprint != "core":
        return None
    if request.endpoint in UNGATED or not current_user.is_authenticated:
        return None
    if next_required(current_user) is None:
        return None
    return redirect(url_for("setup.welcome"))


@bp.get("/welcome")
@login_required
def welcome():
    step = next_required(current_user)
    if step is None:
        # The optional last screen, reached only by being sent here from the
        # one before it. Anyone else who opens /welcome has nothing left to do.
        if request.args.get("step") != "exams":
            return redirect(url_for("core.dashboard"))
        step = "exams"
    return _page(step)


def _page(step: str, errors: dict | None = None, status: int = 200):
    today = date.today()
    settings = settings_for(current_user)
    chosen = current_user.setup_done_at is not None
    return render_template(
        "welcome.html",
        step=step,
        rail=_rail(step),
        errors=errors or {},
        form=request.form,
        # Limits nobody chose yet show as empty boxes, the usual numbers only
        # as placeholders: a pre-filled 75 is a box people tap straight past.
        limits=({"overall": settings.overall_limit, "subject": settings.subject_limit}
                if chosen else {"overall": "", "subject": ""}),
        semester_end=planning.semester_end(current_user),
        suggested_end=(planning.suggested_semester_end(current_user, today)
                       if step == "rules" else None),
        checkpoints=planning.checkpoints_for(current_user),
        today=today,
    ), status


def _rail(step: str) -> list[dict]:
    """Each screen's segment: full once done, half while you're on it.

    A timetable that has to be drawn by hand is part of "Rules" as far as
    the rail is concerned — one extra screen isn't worth a fourth segment.
    """
    here = "rules" if step == "timetable" else step
    keys = [key for key, _ in STEPS]
    at = keys.index(here)
    return [{"key": key, "label": label,
             "state": "done" if i < at else ("now" if i == at else "todo")}
            for i, (key, label) in enumerate(STEPS)]


@bp.post("/welcome/rules")
@login_required
def save_rules():
    """The limits and the semester end, saved together or not at all."""
    errors = apply_limits(settings_for(current_user), request.form, required=True)
    end, end_error = planning.parse_semester_end(request.form.get("end_date"))
    if end_error:
        errors["end_date"] = ("Pick the last day of classes."
                              if not (request.form.get("end_date") or "").strip()
                              else end_error)
    if errors:
        db.session.rollback()
        return _page("rules", errors, 400)

    mark_limits_chosen(current_user)
    # Commits the limits too: one transaction for the whole screen.
    planning.set_semester_end(current_user, end, guess=request.form.get("guess") == "1")
    return redirect(url_for("setup.welcome", step="exams"))


@bp.post("/welcome/finish")
@login_required
def finish():
    tracking.record(current_user, "setup_done")
    db.session.commit()
    flash("You're all set. Here's your day.")
    return redirect(url_for("core.dashboard"))


@bp.app_context_processor
def setup_flags():
    """`setup_attention`: the guessed semester end, while it is still a guess.

    Drives the dot on the Setup tab and the reminder on Today and Plan.
    """
    if not current_user.is_authenticated or not planning.semester_end_is_guess(current_user):
        return {"setup_attention": None}
    return {"setup_attention": planning.semester_end(current_user)}
