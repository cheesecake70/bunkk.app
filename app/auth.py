"""Authentication and account creation — ADR-5.

Registration is open: email, username and a password typed twice. Two things
here only matter because other students share the server — repeated failed
logins lock an account briefly, and identity is claimed rather than assumed
(see merge.py: one student number backs exactly one account).
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, limiter
from .mail import send_mail
from .models import College, Settings, User

bp = Blueprint("auth", __name__)

#: Failed attempts before an account is locked, and for how long.
MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)

#: Letters, digits, underscore and dot — recognisable and safe in a URL later.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{3,32}$")
MIN_PASSWORD = 8

#: Password-reset links. The salt keeps these tokens useless anywhere else a
#: signer might be added later; an hour is long enough to find the mail and
#: short enough that a forwarded inbox isn't a standing key.
RESET_SALT = "password-reset"
RESET_MAX_AGE = 3600

#: How often one address may ask for a link. This throttles our own outgoing
#: mail rather than protecting an account — the reply is identical either way —
#: so a per-process counter is proportionate.
RESET_COOLDOWN = timedelta(minutes=1)
RESET_HOURLY_MAX = 5
_reset_requests: dict[str, list[datetime]] = {}

#: Per-IP ceilings on the three routes that take a guess at a credential.
#: The per-account lockout below is the second line; this one stops a single
#: client trying every account, or minting accounts by the thousand.
#:
#: Sized for a college: a hostel or campus Wi-Fi puts hundreds of students
#: behind one address, so the login ceiling counts *failed* attempts only
#: (`_login_failed`) and the others are loose enough for a whole corridor to
#: sign up in the same hour. A targeted attack on one account is the lockout's
#: job; these stop the spray.
LOGIN_LIMIT = "30 per minute; 300 per hour"
REGISTER_LIMIT = "60 per hour"
FORGOT_LIMIT = "20 per hour"


def _login_failed(response) -> bool:
    return response.status_code in (401, 429)

#: Checked against when the identifier matches nobody, so an unknown email
#: costs the same hash as a wrong password and can't be told apart by timing.
_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))


def password_error(password: str, confirm: str) -> tuple[str, str] | None:
    """`(field, message)` for a new password, or None when it's usable.

    Shared by registration, reset and change so all three agree on what a
    password has to be.
    """
    if len(password) < MIN_PASSWORD:
        return "password", f"Use at least {MIN_PASSWORD} characters."
    if password != confirm:
        return "confirm_password", "Both passwords need to match."
    return None


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
@limiter.limit(REGISTER_LIMIT, methods=["POST"])
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

        problem = password_error(password, confirm)
        if problem:
            errors[problem[0]] = problem[1]

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
@limiter.limit(LOGIN_LIMIT, methods=["POST"], deduct_when=_login_failed)
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

        if user is None:
            check_password_hash(_DUMMY_HASH, password)
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

    # `?as=` prefills the identifier. The upload page sends people here when the
    # report they dropped belongs to their *other* account, and making them
    # remember which one that was is the whole problem it is solving.
    return render_template("auth/login.html", errors={},
                           email=(request.args.get("as") or "").strip()[:255])


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
    redirect, and a login page is exactly where one gets abused.

    `request.values` so a form can carry it too: signing out in order to sign
    into a *particular* other account is a POST, and it has somewhere to be.
    """
    target = request.values.get("next")
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
    return redirect(_safe_next() or url_for("auth.login"))


# ---------------------------------------------------------------------------
# Forgotten passwords
# ---------------------------------------------------------------------------


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.secret_key, salt=RESET_SALT)


def make_reset_token(user: User) -> str:
    """A signed, expiring claim on one account.

    The tail of the password hash rides along so the link stops working the
    moment the password changes — which is what makes it single-use: the first
    successful reset invalidates the mail it came from, and every copy of it.
    """
    return _serializer().dumps({"uid": user.id, "h": user.password_hash[-16:]})


def load_reset_user(token: str) -> User | None:
    try:
        payload = _serializer().loads(token, max_age=RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict):
        return None

    user = db.session.get(User, payload.get("uid"))
    if user is None or payload.get("h") != user.password_hash[-16:]:
        return None
    return user


def _reset_allowed(email: str) -> bool:
    """Throttle our own outgoing mail, per address.

    Deliberately not a security control — the response is identical whether or
    not the address exists — so a per-process record is enough, and a restart
    forgetting it costs nothing.
    """
    now = datetime.now(timezone.utc)

    # Only addresses with an account ever reach this, but the dict would still
    # grow for the lifetime of the process, so expired entries are swept rather
    # than left to accumulate.
    for address in [a for a, times in _reset_requests.items()
                    if not times or now - times[-1] >= timedelta(hours=1)]:
        del _reset_requests[address]

    recent = [t for t in _reset_requests.get(email, []) if now - t < timedelta(hours=1)]
    _reset_requests[email] = recent
    if len(recent) >= RESET_HOURLY_MAX:
        return False
    if recent and now - recent[-1] < RESET_COOLDOWN:
        return False
    recent.append(now)
    return True


@bp.route("/forgot", methods=["GET", "POST"])
@limiter.limit(FORGOT_LIMIT, methods=["POST"])
def forgot():
    """Ask for a reset link.

    Answers the same way for an address with an account and one without: this
    page is otherwise a way to ask the server who has signed up.
    """
    if current_user.is_authenticated:
        return redirect(url_for("core.settings"))

    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = db.session.query(User).filter_by(email=email).first()

        if user is not None and _reset_allowed(email):
            link = url_for("auth.reset", token=make_reset_token(user),
                           _external=True)
            send_mail(
                user.email,
                "Reset your Bunkr password",
                "Someone asked to reset the password for your Bunkr account.\n\n"
                f"Open this link within the hour to choose a new one:\n{link}\n\n"
                "If that wasn't you, ignore this — your password hasn't changed.",
            )

        flash("If that email has an account, a reset link is on its way.", "info")
        return redirect(url_for("auth.login"))

    return render_template("auth/forgot.html", email="")


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset(token: str):
    user = load_reset_user(token)
    if user is None:
        return render_template("auth/reset.html", token=token, expired=True,
                               errors={}), 400

    if request.method == "POST":
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""

        problem = password_error(password, confirm)
        if problem:
            return render_template("auth/reset.html", token=token, expired=False,
                                   errors={problem[0]: problem[1]}), 400

        user.set_password(password)
        # Whoever else was signed in may be why the password is being reset.
        user.rotate_session()
        user.failed_logins = 0
        user.locked_until = None
        db.session.commit()

        login_user(user, remember=True)
        flash("Password changed. Any other devices have been signed out.")
        return redirect(url_for("core.dashboard"))

    return render_template("auth/reset.html", token=token, expired=False, errors={})
