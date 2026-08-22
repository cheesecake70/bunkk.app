"""Authentication and account creation — ADR-5.

Registration is open: email, username and a password typed twice. Two things
here only matter because other students share the server — repeated failed
logins lock an account briefly, and identity is claimed rather than assumed
(see merge.py: one student number backs exactly one account).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from . import db
from .models import College, Settings, User

bp = Blueprint("auth", __name__)

#: Failed attempts before an account is locked, and for how long.
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)

#: Letters, digits, underscore and dot — recognisable and safe in a URL later.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{3,32}$")
MIN_PASSWORD = 8


def normalise_username(raw: str) -> str:
    return (raw or "").strip()


def validate_username(username: str) -> str | None:
    """Returns an error message, or None when the username is usable."""
    if not username:
        return "Pick a username."
    if not USERNAME_RE.match(username):
        return "3–32 characters, using letters, numbers, dots or underscores."
    # Case-insensitive uniqueness: "Aditi" and "aditi" are the same person to
    # everyone except the database.
    taken = (
        db.session.query(User)
        .filter(db.func.lower(User.username) == username.lower())
        .first()
    )
    return "That username is taken." if taken else None


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("core.dashboard"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        username = normalise_username(request.form.get("username"))
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        errors: dict[str, str] = {}

        if not email or "@" not in email:
            errors["email"] = "Enter a valid email address."
        elif db.session.query(User).filter_by(email=email).first():
            errors["email"] = "That email already has an account."

        username_error = validate_username(username)
        if username_error:
            errors["username"] = username_error

        if len(password) < MIN_PASSWORD:
            errors["password"] = f"Use at least {MIN_PASSWORD} characters."
        elif password != confirm:
            errors["confirm_password"] = "Both passwords need to match."

        if errors:
            return render_template(
                "auth/register.html", errors=errors, email=email, username=username,
            ), 400

        college = db.session.query(College).first()
        if college is None:
            # One row from day 1; onboarding another college is additive.
            college = College(name="SVKM")
            db.session.add(college)
            db.session.flush()

        user = User(email=email, username=username, college_id=college.id)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        db.session.add(
            Settings(
                user_id=user.id,
                subject_limit=college.default_subject_limit,
                overall_limit=college.default_overall_limit,
            )
        )
        db.session.commit()

        login_user(user, remember=True)
        return redirect(url_for("core.upload"))

    return render_template("auth/register.html", errors={}, email="", username="")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("core.dashboard"))

    if request.method == "POST":
        identifier = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        user = _find_user(identifier)

        locked_for = _lock_remaining(user)
        if locked_for:
            return render_template(
                "auth/login.html",
                errors={"password": f"Too many attempts. Try again in {locked_for} minutes."},
                email=identifier,
            ), 429

        if user is None or not user.check_password(password):
            _record_failure(user)
            # One message for both cases, so this can't be used to discover
            # which emails have accounts.
            return render_template(
                "auth/login.html",
                errors={"password": "Those details don't match an account."},
                email=identifier,
            ), 401

        user.failed_logins = 0
        user.locked_until = None
        db.session.commit()

        login_user(user, remember=True)
        return redirect(_safe_next() or url_for("core.dashboard"))

    return render_template("auth/login.html", errors={}, email="")


def _find_user(identifier: str) -> User | None:
    """Sign in with either the email or the username — people remember one or
    the other, and there's no reason to make them guess which."""
    if not identifier:
        return None
    return (
        db.session.query(User)
        .filter(
            db.or_(
                User.email == identifier.lower(),
                db.func.lower(User.username) == identifier.lower(),
            )
        )
        .first()
    )


def _safe_next() -> str | None:
    """Only ever redirect within this app — an absolute URL here is an open
    redirect, and a login page is exactly where one gets abused."""
    target = request.args.get("next")
    if not target or not target.startswith("/") or target.startswith("//"):
        return None
    return target


def _lock_remaining(user: User | None) -> int:
    if user is None or user.locked_until is None:
        return 0
    locked_until = user.locked_until
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    remaining = locked_until - datetime.now(timezone.utc)
    return max(0, int(remaining.total_seconds() // 60) + 1) if remaining.total_seconds() > 0 else 0


def _record_failure(user: User | None) -> None:
    if user is None:
        return                      # nothing to count against
    user.failed_logins = (user.failed_logins or 0) + 1
    if user.failed_logins >= MAX_FAILED_LOGINS:
        user.locked_until = datetime.now(timezone.utc) + LOCKOUT
        user.failed_logins = 0
    db.session.commit()


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    flash("Signed out.")
    return redirect(url_for("auth.login"))
