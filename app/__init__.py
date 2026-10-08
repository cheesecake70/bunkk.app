"""Bunkk Flask application factory."""
from __future__ import annotations

import logging
import os
import sqlite3

from urllib.parse import urlsplit

from flask import Flask, flash, jsonify, redirect, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager, login_url
from flask_migrate import Migrate
from flask_sitemap import Sitemap
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import IntegerConverter

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(get_remote_address)


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    """Make SQLite fit for several people at once (ADR-2).

    WAL lets readers carry on while one writer commits, which is exactly this
    workload: constant dashboard reads, occasional upload bursts. Without it a
    single upload blocks every other user's page. The busy timeout turns the
    remaining write contention into a short wait instead of an error, and
    foreign keys stop a deleted account leaving orphaned lectures behind.

    The busy timeout is the length of the longest write queue the app will
    wait out. Writes are rare (an upload, a plan) and reads never wait on them
    in WAL mode, so a long wait costs nothing on a normal day — while a short
    one turned a hostel's worth of simultaneous uploads into "database is
    locked" errors under load testing. Kept under gunicorn's request timeout.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class RowIdConverter(IntegerConverter):
    """`<int:...>` in a URL, capped at what a database id can be.

    Werkzeug's own accepts any run of digits, and a thirty-digit "id" then
    reaches the driver as a number it cannot bind — a 500 for what is plainly
    a page that doesn't exist. Out of range, the URL simply doesn't match.
    """

    def __init__(self, map, fixed_digits=0, min=None, max=None, signed=False):
        super().__init__(map, fixed_digits=fixed_digits, min=min,
                         max=2 ** 63 - 1 if max is None else max, signed=signed)


def create_app(config_object=None) -> Flask:
    # Templates and static assets live at the repo root, beside the app package.
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder="../templates",
        static_folder="../static",
    )

    app.config.from_object(
        config_object or os.environ.get("BUNKK_CONFIG") or "config.DevConfig"
    )
    app.url_map.converters["int"] = RowIdConverter
    os.makedirs(app.instance_path, exist_ok=True)
    _check_environment(app)
    _configure_logging(app)

    if app.config.get("BEHIND_PROXY"):
        # One trusted hop: the reverse proxy's X-Forwarded-For/-Proto/-Host
        # become the request's own, so rate limits key on the real client and
        # external links come out https.
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    limiter.init_app(app)
    login_manager.init_app(app)
    # Absolute URLs in /sitemap.xml follow the site's own scheme (https in
    # production); Flask-Sitemap would otherwise default to http.
    app.config["SITEMAP_URL_SCHEME"] = app.config.get("PREFERRED_URL_SCHEME", "http")
    sitemap = Sitemap(app)   # per app: the instance keeps its generators
    login_manager.login_view = "auth.login"
    # "Please log in" is not good news; it was rendering in the green success
    # banner because that is what an uncategorised flash falls back to.
    login_manager.login_message_category = "info"

    from . import models  # noqa: F401  (register models with SQLAlchemy)

    @login_manager.user_loader
    def load_user(token: str):
        """Sessions carry a random token, never the row id (see models.User).

        Cookies issued before the token existed hold an integer, which matches
        no token, so they resolve to nobody and the holder is asked to sign in
        again — the one-off cost of closing the id-reuse hole.
        """
        return (
            db.session.query(models.User)
            .filter_by(session_token=token)
            .one_or_none()
        )

    @login_manager.unauthorized_handler
    def unauthorized():
        # fetch() callers can act on a 401; a redirect to an HTML login page
        # only ever reached them as "unexpected error".
        if _wants_json():
            return jsonify(error="Please sign in again."), 401
        flash(login_manager.login_message, login_manager.login_message_category)
        return redirect(login_url(login_manager.login_view, request.url))

    from . import filters
    filters.register(app)

    from .account import bp as account_bp
    from .api import bp as api_bp
    from . import auth, tracking
    from .routes import bp as core_bp

    auth.init_app(app)
    tracking.init_app(app)

    app.register_blueprint(core_bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(account_bp)

    _register_sitemap(sitemap)
    _register_error_handlers(app)
    _register_security_headers(app)
    return app


def _register_sitemap(sitemap: Sitemap) -> None:
    """/sitemap.xml lists the pages a signed-out visitor, or a crawler, can see.

    Listed by name rather than by walking the URL map: nearly every other route
    sits behind sign-in and belongs to one student, so the extension's "include
    every route" switch would advertise pages that only redirect to the login.
    """
    @sitemap.register_generator
    def public_pages():
        yield "core.dashboard", {}, None, "monthly", 1.0   # the landing page
        yield "auth.register", {}, None, "yearly", 0.5
        yield "auth.login", {}, None, "yearly", 0.3


def _check_environment(app: Flask) -> None:
    """Refuse to serve real users on a configuration that only looks complete.

    Every one of these fails quietly otherwise: the dev secret forges sessions,
    a missing DATABASE_URL lands the ledger in the repo directory, no Google
    client means nobody can sign in at all, and no trusted host lets a forged
    Host header write the OAuth redirect URL.
    """
    from config import DEV_SECRET_KEY, MIN_SECRET_KEY_LENGTH, PLACEHOLDER_SECRET_KEYS

    missing = [name for name in app.config.get("REQUIRED_ENV", ())
               if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing)
            + ". See .env.example."
        )

    if app.debug or app.testing:
        return

    generate = (
        "Generate one with "
        "`python -c \"import secrets; print(secrets.token_hex(32))\"` and "
        "set it in the environment before serving real users."
    )
    secret = app.config["SECRET_KEY"] or ""
    if secret == DEV_SECRET_KEY or secret in PLACEHOLDER_SECRET_KEYS:
        raise RuntimeError("SECRET_KEY is still the development default. " + generate)
    if len(secret) < MIN_SECRET_KEY_LENGTH:
        raise RuntimeError(
            f"SECRET_KEY is shorter than {MIN_SECRET_KEY_LENGTH} characters, "
            "which is short enough to guess. " + generate
        )


def _configure_logging(app: Flask) -> None:
    """Logs go wherever gunicorn's do (stderr, for the process manager to
    collect), and nothing is dropped at INFO."""
    if app.testing:
        return
    gunicorn_logger = logging.getLogger("gunicorn.error")
    if gunicorn_logger.handlers:
        app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(logging.INFO)


def _wants_json() -> bool:
    return request.path.startswith("/api/") or request.is_json


def _same_site(url: str | None) -> str | None:
    """The path of `url` when it points at this app, else None.

    The Referer is whatever the previous page said it was. Bouncing a failed
    form back to it is right when that page is ours and an open redirect when
    it is not — and a cross-site form is exactly what a CSRF failure implies.
    """
    if not url:
        return None
    parts = urlsplit(url)
    if parts.netloc and parts.netloc != request.host:
        return None
    path = parts.path or "/"
    if not path.startswith("/") or path.startswith("//") or "\\" in path:
        return None
    return path + (f"?{parts.query}" if parts.query else "")


def _register_error_handlers(app: Flask) -> None:
    @app.errorhandler(CSRFError)
    def csrf_failed(error):
        if _wants_json():
            return jsonify(error="Your session expired. Reload the page and try again."), 400
        flash("That form had expired. Please try again.", "error")
        return redirect(_same_site(request.referrer) or "/")

    @app.errorhandler(HTTPException)
    def http_error(error):
        # HTML pages keep Werkzeug's own error pages; fetch() callers need JSON.
        # The one exception is the throttle, which a person can hit from a
        # sign-in form and deserves a sentence rather than a status code.
        if not _wants_json():
            if error.code == 429:
                return (
                    "<!doctype html><title>Slow down</title><h1>Too many attempts</h1>"
                    "<p>Wait a minute and try again.</p>", 429,
                    {"Retry-After": "60"},
                )
            return error
        message = error.description
        if error.code == 413:
            message = "That file is too large to be an attendance report."
        elif error.code == 429:
            message = "Too many requests. Wait a minute and try again."
        return jsonify(error=message), error.code

    @app.errorhandler(OperationalError)
    def database_busy(error):
        # The one operational error worth a sentence of its own: SQLite's
        # single writer is still busy after the whole busy timeout. That is a
        # queue, not a bug, and the right answer is "try again in a moment".
        if "locked" not in str(error.orig or error).lower():
            raise error
        db.session.rollback()
        app.logger.warning("Write lock still held after busy timeout on %s %s",
                           request.method, request.path)
        message = "Bunkk is busy saving other students' reports. Try again in a few seconds."
        headers = {"Retry-After": "5"}
        if _wants_json():
            return jsonify(error=message), 503, headers
        return (f"<!doctype html><title>Busy</title><h1>One moment</h1><p>{message}</p>",
                503, headers)

    @app.errorhandler(IntegrityError)
    def clashed(error):
        # Two requests wrote the same thing at once and a unique index let one
        # through. The endpoints where that is routine retry by themselves
        # (api.idempotent); this is the answer for the rest — a username both
        # of two people just picked, say.
        db.session.rollback()
        app.logger.warning("Write clashed on %s %s: %s",
                           request.method, request.path, error.orig)
        message = "That clashed with another change. Reload and try again."
        if _wants_json():
            return jsonify(error=message), 409
        flash(message, "error")
        return redirect(_same_site(request.referrer) or "/")

    if app.testing:
        # Tests want the traceback, not a tidy 500.
        return

    @app.errorhandler(Exception)
    def unexpected(error):
        app.logger.exception("Unhandled error on %s %s", request.method, request.path)
        if _wants_json():
            return jsonify(error="Something went wrong on our side. Try again in a moment."), 500
        return (
            "<!doctype html><title>Something went wrong</title>"
            "<h1>Something went wrong</h1><p>It has been logged. Please try again.</p>",
            500,
        )


def _register_security_headers(app: Flask) -> None:
    hsts = bool(app.config.get("SESSION_COOKIE_SECURE"))

    @app.after_request
    def add_headers(response):
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        # Inline scripts and styles are part of the templates, so the policy
        # limits where things load from rather than banning inline code.
        headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; "
            "object-src 'none'",
        )
        # Pages and API answers are one student's attendance. Without this a
        # shared laptop shows them again on Back after signing out, straight
        # from the browser's cache. Static files keep their normal caching.
        if request.endpoint != "static":
            headers.setdefault("Cache-Control", "no-store")
        if hsts:
            headers.setdefault("Strict-Transport-Security",
                               "max-age=31536000; includeSubDomains")
        return response
