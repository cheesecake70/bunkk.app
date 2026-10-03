"""Authentication — Google sign-in only (ADR-5, revised).

There is no password anywhere in Bunkk. Signing in means proving to Google
that you own an address, and Google telling us so in a signed ID token. That
one decision removes every password-shaped problem this module used to carry:
no hashes, no lockouts, no reset mail, no verification links, no dummy-hash
timing games. What is left is small — start the OpenID Connect dance, finish
it, and map the address Google vouches for onto one Bunkk account.

Which *student* an account is for comes from the first report uploaded into
it (merge.py); this module only settles who is at the keyboard.
"""
from __future__ import annotations

import re

from authlib.integrations.flask_client import OAuth
from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy.exc import IntegrityError

from . import db, limiter
from .models import College, Settings, User

bp = Blueprint("auth", __name__)

#: Google publishes its endpoints at the discovery URL, so nothing else about
#: Google is hard-coded. The client lives on the app (`init_app`), not at
#: module level, so every `create_app` gets its own.
GOOGLE_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"
OAUTH_EXT = "bunkk_oauth"

#: Letters, digits, underscore and dot — recognisable and safe in a URL later.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{3,32}$")

#: Where `next` waits while the browser is off at Google.
NEXT_KEY = "auth.next"

#: Per-IP ceiling on starting a sign-in. Each start is a redirect to Google,
#: which costs us nothing, so this only stops a script hammering the route.
LOGIN_LIMIT = "60 per minute"

#: Seconds to wait on Google (discovery document, token exchange, signing
#: keys). Without one a stalled connection holds a worker thread indefinitely.
GOOGLE_TIMEOUT = 10


class AccountConflict(Exception):
    """The address Google vouched for already belongs to a different Google
    identity's Bunkk account."""


def init_app(app) -> None:
    oauth = OAuth(app)
    app.extensions[OAUTH_EXT] = oauth
    oauth.register(
        name="google",
        client_id=app.config.get("GOOGLE_CLIENT_ID"),
        client_secret=app.config.get("GOOGLE_CLIENT_SECRET"),
        server_metadata_url=GOOGLE_DISCOVERY,
        client_kwargs={"scope": "openid email profile",
                       "default_timeout": GOOGLE_TIMEOUT},
    )


def _google():
    return current_app.extensions[OAUTH_EXT].google


# ---------------------------------------------------------------------------
# Usernames
# ---------------------------------------------------------------------------


def normalise_username(raw: str) -> str:
    return (raw or "").strip()


def validate_username(username: str) -> str | None:
    """Returns an error message, or None when the username is usable."""
    if not username:
        return "Pick a username."
    if not USERNAME_RE.match(username):
        return "3–32 characters, using letters, numbers, dots or underscores."
    return "That username is taken." if _username_taken(username) else None


def _username_taken(username: str) -> bool:
    # Case-insensitive: "Aditi" and "aditi" are the same person to everyone
    # except the database.
    return (
        db.session.query(User.id)
        .filter(db.func.lower(User.username) == username.lower())
        .first()
    ) is not None


def suggest_username(email: str) -> str:
    """A first username from the address, made unique with a numeric tail.

    Nobody is asked to invent a name at sign-up any more — the Google button
    is the whole form — so the local part of the address stands in until
    they change it on the Settings page.
    """
    base = re.sub(r"[^A-Za-z0-9_.]", "", email.split("@")[0])[:28] or "student"
    base = base.ljust(3, "0")
    candidate = base
    n = 1
    while _username_taken(candidate):
        n += 1
        candidate = f"{base}{n}"
    return candidate


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@bp.get("/login")
def login():
    """The sign-in page: one button. `?as=` is the address the upload page
    thinks this report belongs to; it becomes Google's `login_hint`, so the
    account picker opens on the right person."""
    if current_user.is_authenticated:
        return redirect(url_for("core.dashboard"))
    return render_template("auth/login.html",
                           email=(request.args.get("as") or "").strip()[:255])


@bp.get("/register")
def register():
    # There is no separate sign-up: the first Google sign-in creates the account.
    return redirect(url_for("auth.login", **request.args))


@bp.get("/login/google")
@limiter.limit(LOGIN_LIMIT)
def google_start():
    if current_user.is_authenticated:
        return redirect(url_for("core.dashboard"))
    target = _safe_next()
    if target:
        session[NEXT_KEY] = target
    else:
        session.pop(NEXT_KEY, None)

    params = {}
    hint = (request.args.get("as") or "").strip()[:255]
    if hint:
        params["login_hint"] = hint
    redirect_uri = url_for("auth.google_callback", _external=True)
    try:
        return _google().authorize_redirect(redirect_uri, **params)
    except Exception:                       # noqa: BLE001 — network, or no client configured
        # The redirect needs Google's discovery document. If Google can't be
        # reached, say so on the page they came from rather than with a 500.
        current_app.logger.warning("could not start google sign-in", exc_info=True)
        flash("Couldn't reach Google to sign you in. Try again in a moment.", "error")
        return redirect(url_for("auth.login"))


@bp.get("/auth/google/callback")
def google_callback():
    """Finish the dance. Authlib checks `state` against the session and
    verifies the ID token's signature, issuer, audience and expiry before
    we ever look at the claims inside."""
    if request.args.get("error"):
        # The person pressed Cancel on Google's screen. Not an error of ours.
        flash("Sign-in was cancelled.", "info")
        return redirect(url_for("auth.login"))

    try:
        claims = _google_userinfo()
    except Exception:                       # noqa: BLE001 — anything Authlib raises
        current_app.logger.warning("google sign-in failed", exc_info=True)
        flash("Google didn't sign you in. Try again.", "error")
        return redirect(url_for("auth.login"))

    email = (claims.get("email") or "").strip().lower()
    sub = claims.get("sub")
    if not sub or not email or not claims.get("email_verified", False):
        # Google only vouches for verified addresses; an unverified one is
        # exactly the squatting risk the old mailed link existed to close.
        flash("Google hasn't verified that email address.", "error")
        return redirect(url_for("auth.login"))

    try:
        user, created = _user_for(sub, email)
    except AccountConflict:
        current_app.logger.warning("google sign-in refused: address held by another identity")
        flash("That email address is already linked to a different Google account.",
              "error")
        return redirect(url_for("auth.login"))
    if current_user.is_authenticated and current_user.id != user.id:
        logout_user()
    login_user(user, remember=True)

    target = session.pop(NEXT_KEY, None)
    if created:
        flash(f"Welcome to Bunkk. You're @{user.username} — change that any time in Settings.")
        return redirect(url_for("core.upload"))
    return redirect(target or url_for("core.dashboard"))


@bp.route("/login/dev", methods=["GET", "POST"])
def dev_login():
    """Sign in as any address without Google — development only.

    Exists so a laptop with no OAuth client, and the load-test harness, can
    still get past the front door. Refused unless the app is in DEBUG *and*
    `BUNKK_DEV_LOGIN=1` is set: two switches, because one of them being on
    in production would otherwise be a wide-open door.
    """
    if not (current_app.debug and current_app.config.get("DEV_LOGIN")):
        abort(404)
    if request.method == "GET":
        return render_template("auth/dev_login.html")
    email = (request.form.get("email") or "").strip().lower()
    if not email or "@" not in email:
        flash("Enter an email address.", "error")
        return redirect(url_for("auth.dev_login"))
    try:
        user, created = _user_for("dev-" + email, email)
    except AccountConflict:
        flash("That address belongs to an account that signs in with Google.", "error")
        return redirect(url_for("auth.dev_login"))
    login_user(user, remember=True)
    return redirect(url_for("core.upload" if created else "core.dashboard"))


def _google_userinfo() -> dict:
    """The verified claims from Google's ID token. Kept as one small function
    so tests can stand in for Google without a network."""
    token = _google().authorize_access_token()
    return dict(token.get("userinfo") or {})


def _user_for(sub: str, email: str) -> tuple[User, bool]:
    """The account this Google identity signs into, creating it on first use.

    Matched on Google's `sub` first — it never changes, an address can. An
    account that predates Google sign-in has no `sub` yet and is matched on
    its address once, which is how everyone already registered keeps their
    ledger.

    Raises `AccountConflict` when the address belongs to an account that a
    *different* Google identity already owns. Addresses get reassigned — a
    college hands last year's mailbox to a new student — and adopting on the
    address alone would hand them the previous holder's ledger with it.
    """
    try:
        return _find_or_create_user(sub, email)
    except IntegrityError:
        # Two first sign-ins for one person landed together (a double-clicked
        # button, two tabs); the unique indexes let exactly one through. The
        # other finds the row the first one made.
        db.session.rollback()
        user = db.session.query(User).filter_by(google_sub=sub).first()
        if user is None:
            raise
        return user, False


def _find_or_create_user(sub: str, email: str) -> tuple[User, bool]:
    user = db.session.query(User).filter_by(google_sub=sub).first()
    if user is not None:
        if user.email != email:
            # Follow a rename at Google — unless another account still holds
            # the new address, in which case keeping the old one costs nothing.
            taken = db.session.query(User.id).filter_by(email=email).first()
            if taken is None:
                user.email = email
                db.session.commit()
        return user, False

    user = db.session.query(User).filter_by(email=email).first()
    if user is not None:
        if user.google_sub is not None:
            raise AccountConflict(email)
        user.google_sub = sub
        user.mark_verified()
        db.session.commit()
        return user, False

    college = db.session.query(College).first()
    if college is None:
        # One row from day 1; onboarding another college is additive.
        college = College(name="SVKM")
        db.session.add(college)
        db.session.flush()

    user = User(email=email, google_sub=sub, username=suggest_username(email),
                college_id=college.id)
    user.mark_verified()
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
    return user, True


def _safe_next() -> str | None:
    """Only ever redirect within this app — an absolute URL here is an open
    redirect, and a login page is exactly where one gets abused.

    `request.values` so a form can carry it too: signing out in order to sign
    into a *particular* other account is a POST, and it has somewhere to be.
    """
    target = request.values.get("next")
    if not target or not target.startswith("/") or target.startswith("//"):
        return None
    # Browsers read a backslash as a slash, so "/\\host" is "//host" by
    # another name; control characters have no business in a path either.
    if "\\" in target or any(ord(ch) < 0x20 for ch in target):
        return None
    return target[:2000]


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    flash("Signed out.")
    return redirect(_safe_next() or url_for("auth.login"))
