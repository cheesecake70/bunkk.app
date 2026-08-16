"""Authentication and account creation — ADR-5, extended for real multi-user.

Three things change once other students have accounts on the same box:

  1. Registration is gated. A self-hosted app on a public URL shouldn't hand
     accounts to anyone who finds it, so new users arrive by invite. The first
     account is exempt — someone has to be able to start.
  2. Repeated failed logins lock an account briefly, so a weak password isn't
     open to unlimited guessing.
  3. Identity is claimed, not assumed (see merge.py): a student number belongs
     to exactly one account.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from . import db
from .models import College, Invite, Settings, User

bp = Blueprint("auth", __name__)

#: Failed attempts before an account is locked, and for how long.
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)


def registration_mode() -> str:
    return current_app.config.get("REGISTRATION", "invite")


def first_account() -> bool:
    return db.session.query(User).count() == 0


def new_invite_code() -> str:
    return secrets.token_urlsafe(9)


def _consume_invite(code: str) -> Invite | None:
    if not code:
        return None
    invite = db.session.query(Invite).filter_by(code=code.strip()).one_or_none()
    if invite is None or invite.is_used:
        return None
    return invite


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("core.dashboard"))

    mode = registration_mode()
    bootstrap = first_account()
    needs_invite = mode == "invite" and not bootstrap

    if mode == "closed" and not bootstrap:
        return render_template("auth/closed.html"), 403

    prefill = request.args.get("invite", "")

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        code = (request.form.get("invite") or "").strip()
        errors: dict[str, str] = {}

        invite = None
        if needs_invite:
            invite = _consume_invite(code)
            if invite is None:
                errors["invite"] = "That invite code isn't valid or has already been used."

        if not email or "@" not in email:
            errors["email"] = "Enter a valid email address."
        elif db.session.query(User).filter_by(email=email).first():
            errors["email"] = "That email already has an account."
        if len(password) < 8:
            errors["password"] = "Use at least 8 characters."

        if errors:
            return render_template(
                "auth/register.html", errors=errors, email=email,
                needs_invite=needs_invite, invite=code, bootstrap=bootstrap,
            ), 400

        college = db.session.query(College).first()
        if college is None:
            # One row from day 1; onboarding another college is additive.
            college = College(name="SVKM")
            db.session.add(college)
            db.session.flush()

        user = User(email=email, college_id=college.id)
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
        if invite is not None:
            invite.used_by_id = user.id
            invite.used_at = datetime.now(timezone.utc)
        db.session.commit()

        login_user(user, remember=True)
        return redirect(url_for("core.upload"))

    return render_template(
        "auth/register.html", errors={}, email="",
        needs_invite=needs_invite, invite=prefill, bootstrap=bootstrap,
    )


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("core.dashboard"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user = db.session.query(User).filter_by(email=email).first()

        locked_for = _lock_remaining(user)
        if locked_for:
            return render_template(
                "auth/login.html",
                errors={"password": f"Too many attempts. Try again in {locked_for} minutes."},
                email=email,
            ), 429

        if user is None or not user.check_password(password):
            _record_failure(user)
            # One message for both cases, so this can't be used to discover
            # which emails have accounts.
            return render_template(
                "auth/login.html",
                errors={"password": "Email or password is wrong."},
                email=email,
            ), 401

        user.failed_logins = 0
        user.locked_until = None
        db.session.commit()

        login_user(user, remember=True)
        return redirect(_safe_next() or url_for("core.dashboard"))

    return render_template("auth/login.html", errors={}, email="")


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
