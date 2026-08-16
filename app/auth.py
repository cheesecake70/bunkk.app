"""Authentication — ADR-5: real accounts from day 1, one tenant in practice.

Every user-owned row keys on `user_id` already, so opening this up later needs
a signup flow and an isolation review, not a schema migration.
"""
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from . import db
from .models import College, Settings, User

bp = Blueprint("auth", __name__)


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("core.dashboard"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        errors = {}

        if not email or "@" not in email:
            errors["email"] = "Enter a valid email address."
        elif db.session.query(User).filter_by(email=email).first():
            errors["email"] = "That email already has an account."
        if len(password) < 8:
            errors["password"] = "Use at least 8 characters."

        if errors:
            return render_template("auth/register.html", errors=errors, email=email), 400

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
        db.session.commit()

        login_user(user, remember=True)
        return redirect(url_for("core.upload"))

    return render_template("auth/register.html", errors={}, email="")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("core.dashboard"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        user = db.session.query(User).filter_by(email=email).first()

        if user is None or not user.check_password(password):
            return render_template(
                "auth/login.html",
                errors={"password": "Email or password is wrong."},
                email=email,
            ), 401

        login_user(user, remember=True)
        return redirect(request.args.get("next") or url_for("core.dashboard"))

    return render_template("auth/login.html", errors={}, email="")


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    flash("Signed out.")
    return redirect(url_for("auth.login"))
