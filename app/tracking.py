"""Usage tracking — who signed in, who came back, and what they used.

First-party and in the database on purpose. The Content-Security-Policy only
loads scripts from this origin, so a hosted tracker would need a hole cut in
it, and would hand a third party a record of which students open the app. A
couple of timestamps and a thin event table answer the same questions.

Two rules keep it from costing anything:

* `record` never commits. It rides in the transaction of the action it
  describes, so an upload is still one write, and an action that rolls back
  takes its event with it.
* The "last seen" stamp is written at most once per `SEEN_EVERY` per person.
  SQLite has one writer, and a write on every page view would queue everyone's
  dashboard behind everyone else's.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import click
from flask import Blueprint, abort, current_app, render_template, request
from flask_login import current_user, login_required
from sqlalchemy.exc import SQLAlchemyError

from . import db
from .models import Event, User, utcnow

bp = Blueprint("admin", __name__, url_prefix="/admin")

#: How stale `last_seen_at` may get before a request refreshes it.
SEEN_EVERY = timedelta(hours=1)

#: Requests that say nothing about whether a person is using the app.
UNTRACKED_ENDPOINTS = frozenset({"static", "core.healthz"})

#: The days the stats page tabulates.
DAILY_ROWS = 14


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands datetimes back without their zone; they were stored as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def record(user, name: str) -> None:
    """Note that `user` did `name`. The caller's commit is what stores it."""
    db.session.add(Event(user_id=user.id, name=name))


def _mark_seen(user, now: datetime) -> None:
    """Stamp `last_seen_at`, and count the first sighting of each UTC day as a
    `visit` — the row daily-active and retention figures are built from."""
    seen = _aware(user.last_seen_at)
    if seen is None or seen.date() != now.date():
        record(user, "visit")
    user.last_seen_at = now


def note_login(user, *, created: bool) -> None:
    """A sign-in just succeeded. Commits, since nothing else on this path will."""
    now = utcnow()
    if created:
        record(user, "signup")
    record(user, "login")
    user.last_login_at = now
    _mark_seen(user, now)
    db.session.commit()


def _touch() -> None:
    if request.endpoint is None or request.endpoint in UNTRACKED_ENDPOINTS:
        return
    if not current_user.is_authenticated:
        return
    now = utcnow()
    seen = _aware(current_user.last_seen_at)
    if seen is not None and now - seen < SEEN_EVERY:
        return
    try:
        _mark_seen(current_user, now)
        db.session.commit()
    except SQLAlchemyError:
        # A busy database must never cost someone their page; the next request
        # will try again.
        db.session.rollback()
        current_app.logger.warning("could not record last-seen", exc_info=True)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def summary(now: datetime | None = None) -> dict:
    """Everything the stats page and `flask stats` show, as plain data."""
    now = now or utcnow()
    windows = {"1d": now - timedelta(days=1), "7d": now - timedelta(days=7),
               "30d": now - timedelta(days=30)}

    def users_since(column, cutoff) -> int:
        return db.session.query(User.id).filter(column >= cutoff).count()

    total = db.session.query(User.id).count()
    with_report = (
        db.session.query(User.id).filter(User.student_number.isnot(None)).count()
    )

    events = []
    names = [n for (n,) in db.session.query(Event.name).distinct().order_by(Event.name)]
    for name in names:
        base = db.session.query(Event).filter(Event.name == name)
        recent = base.filter(Event.created_at >= windows["30d"])
        events.append({
            "name": name,
            "last_7d": base.filter(Event.created_at >= windows["7d"]).count(),
            "last_30d": recent.count(),
            "users_30d": recent.with_entities(Event.user_id).distinct().count(),
            "all_time": base.count(),
        })

    return {
        "generated_at": now,
        "users": {
            "total": total,
            "with_report": with_report,
            "signups": {k: users_since(User.created_at, v) for k, v in windows.items()},
            "active": {k: users_since(User.last_seen_at, v) for k, v in windows.items()},
        },
        "events": events,
        "daily": _daily(now),
    }


def _daily(now: datetime) -> list[dict]:
    """One row per UTC day, newest first: who showed up and what happened."""
    first = (now - timedelta(days=DAILY_ROWS - 1)).date()
    start = datetime(first.year, first.month, first.day, tzinfo=timezone.utc)
    day = db.func.date(Event.created_at)
    rows = (
        db.session.query(day, Event.name, db.func.count(Event.id),
                         db.func.count(db.distinct(Event.user_id)))
        .filter(Event.created_at >= start)
        .group_by(day, Event.name)
        .all()
    )
    by_day: dict[str, dict[str, tuple[int, int]]] = {}
    for on, name, count, users in rows:
        by_day.setdefault(str(on), {})[name] = (count, users)

    out = []
    for offset in range(DAILY_ROWS):
        key = (now.date() - timedelta(days=offset)).isoformat()
        got = by_day.get(key, {})
        out.append({
            "date": key,
            "active": got.get("visit", (0, 0))[1],
            "signups": got.get("signup", (0, 0))[0],
            "logins": got.get("login", (0, 0))[0],
            "uploads": got.get("report_uploaded", (0, 0))[0],
        })
    return out


def recent_users(limit: int = 50) -> list[User]:
    """Accounts by most recent activity; never-seen ones last."""
    return (
        db.session.query(User)
        .order_by(User.last_seen_at.is_(None), User.last_seen_at.desc(),
                  User.created_at.desc())
        .limit(limit)
        .all()
    )


# ---------------------------------------------------------------------------
# Surfaces
# ---------------------------------------------------------------------------


def is_admin(user) -> bool:
    return (user.email or "").lower() in current_app.config.get("ADMIN_EMAILS", ())


@bp.get("/stats")
@login_required
def stats():
    # 404 rather than 403: to everyone else this page does not exist.
    if not is_admin(current_user):
        abort(404)
    return render_template("admin_stats.html", stats=summary(), users=recent_users())


def _print_summary(data: dict) -> None:
    users = data["users"]
    click.echo(f"Bunkk usage — {data['generated_at']:%Y-%m-%d %H:%M} UTC\n")
    click.echo(f"Accounts          {users['total']}"
               f"   (with a report: {users['with_report']})")
    for label, key in (("New accounts", "signups"), ("Active accounts", "active")):
        w = users[key]
        click.echo(f"{label:<17} {w['1d']} today · {w['7d']} in 7d · {w['30d']} in 30d")

    click.echo("\nEvents               7d    30d  users(30d)  all-time")
    for e in data["events"]:
        click.echo(f"  {e['name']:<17} {e['last_7d']:>5} {e['last_30d']:>6} "
                   f"{e['users_30d']:>11} {e['all_time']:>9}")
    if not data["events"]:
        click.echo("  (none yet)")

    click.echo("\nDay          active  signups  logins  uploads")
    for d in data["daily"]:
        click.echo(f"  {d['date']} {d['active']:>7} {d['signups']:>8} "
                   f"{d['logins']:>7} {d['uploads']:>8}")


def init_app(app) -> None:
    app.before_request(_touch)
    app.register_blueprint(bp)

    @app.cli.command("stats")
    def stats_command():
        """Print sign-ups, active accounts and feature usage."""
        _print_summary(summary())
