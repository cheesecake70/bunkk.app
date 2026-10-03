"""Configuration objects. Select with BUNKR_CONFIG env var.

A `.env` file beside this module is loaded first, so a deployment only ever
has to fill in `.env.example`. Real environment variables win over the file.
"""
import os
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

BASEDIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASEDIR, ".env"))


def _set_timezone() -> None:
    """Make "today" mean today where the college is, not where the server is.

    Every verdict hangs on `date.today()`. A server left on UTC is five and a
    half hours behind Mumbai, so from midnight until 05:30 it would answer
    "can I skip today?" about yesterday. The process's own zone is set here,
    once, so nothing downstream has to think about it.
    """
    name = os.environ.get("BUNKR_TIMEZONE") or "Asia/Kolkata"
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RuntimeError(
            f"BUNKR_TIMEZONE={name!r} is not a timezone this machine knows. "
            "Use an IANA name such as Asia/Kolkata."
        ) from exc
    os.environ["TZ"] = name
    if hasattr(time, "tzset"):          # not on Windows; the server is Linux
        time.tzset()


_set_timezone()


#: Anyone who knows this can forge another user's session, so production
#: refuses to start with it.
DEV_SECRET_KEY = "dev-only-change-me"
#: Values that have appeared in this repository's examples. Each is public, so
#: each is as forgeable as the default above.
PLACEHOLDER_SECRET_KEYS = frozenset({
    "dev-secret-key-change-in-production",
    "change-me",
    "changeme",
})
MIN_SECRET_KEY_LENGTH = 32


def _bool(name: str, default: str) -> bool:
    return os.environ.get(name, default) not in ("0", "false", "False", "")


def _path(value: str) -> str:
    """A filesystem path from the environment, anchored to the repo.

    A relative path would otherwise mean "relative to wherever the process was
    started", which is one directory under `python run.py` and another under
    a process manager.
    """
    return value if os.path.isabs(value) else os.path.join(BASEDIR, value)


def _database_url(value: str) -> str:
    """Normalise DATABASE_URL into something SQLAlchemy opens as written.

    Two spellings bite at deploy time. A relative SQLite path
    (`sqlite:///instance/bunkr.db`) is resolved by Flask-SQLAlchemy against
    the instance folder, landing in `instance/instance/` — a directory that
    doesn't exist. And hosting platforms hand out `postgres://`, a scheme
    SQLAlchemy stopped accepting.
    """
    if value.startswith("postgres://"):
        return "postgresql://" + value[len("postgres://"):]
    prefix = "sqlite:///"
    if value.startswith(prefix):
        path = value[len(prefix):]
        if path and path != ":memory:" and not path.startswith(("/", "file:")):
            return prefix + os.path.join(BASEDIR, path)
    return value


class BaseConfig:
    SECRET_KEY = os.environ.get("SECRET_KEY") or DEV_SECRET_KEY
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_DIR = _path(
        os.environ.get("BUNKR_UPLOAD_DIR") or os.path.join("instance", "uploads")
    )
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB — reports are ~25-400 KB

    # CSRF tokens are bound to the session, not the clock: a page left open
    # over lunch must still be able to submit.
    WTF_CSRF_TIME_LIMIT = None

    # Throttles on starting a sign-in (per IP) and on uploads (per account).
    # memory:// is per process; point this at Redis when running several
    # gunicorn workers, otherwise every worker gets its own allowance.
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI") or "memory://"
    RATELIMIT_HEADERS_ENABLED = True

    # Google sign-in (the only way in). Created in Google Cloud Console; see
    # README "Google sign-in". Missing in dev, the app still boots and the
    # sign-in button says it couldn't reach Google; use BUNKR_DEV_LOGIN there.
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
    #: /login/dev — sign in as any address without Google. Only honoured in
    #: DEBUG (see auth.dev_login); a laptop without an OAuth client needs it.
    DEV_LOGIN = _bool("BUNKR_DEV_LOGIN", "0")

    #: Environment variables `create_app` insists on. Empty outside production.
    REQUIRED_ENV: tuple[str, ...] = ()
    #: Trust X-Forwarded-* from one hop (nginx / Caddy in front of gunicorn).
    BEHIND_PROXY = False


class DevConfig(BaseConfig):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = _database_url(
        os.environ.get("DATABASE_URL") or "sqlite:///instance/bunkr.db"
    )


class TestConfig(BaseConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite://"  # in-memory
    GOOGLE_CLIENT_ID = "test-client-id"
    GOOGLE_CLIENT_SECRET = "test-client-secret"
    # Both are exercised by dedicated tests that switch them back on; every
    # other test would otherwise have to carry a token and share a counter.
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False


class ProdConfig(BaseConfig):
    DEBUG = False
    # /login/dev already needs debug mode, which production never runs in.
    # Off here as well, so no single slip can open a password-free front door.
    DEV_LOGIN = False
    SQLALCHEMY_DATABASE_URI = _database_url(os.environ.get("DATABASE_URL", ""))
    # Self-hosted deployment: gunicorn behind nginx/Caddy with HTTPS.
    BEHIND_PROXY = _bool("BUNKR_BEHIND_PROXY", "1")
    PREFERRED_URL_SCHEME = "https"
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # Flask-Login's remember-me cookie has its own switches and its own
    # defaults (not Secure, no SameSite), so it needs saying separately.
    REMEMBER_COOKIE_SECURE = True
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    # Hosts this app will answer for. Anything else is refused before a
    # OAuth redirect URL can be built from a forged Host header.
    TRUSTED_HOSTS = [
        h.strip() for h in os.environ.get("BUNKR_TRUSTED_HOSTS", "").split(",")
        if h.strip()
    ] or None
    REQUIRED_ENV = (
        "SECRET_KEY", "DATABASE_URL", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
        "BUNKR_TRUSTED_HOSTS",
    )
    # The boot-time guard lives in create_app: Flask's from_object reads class
    # attributes without instantiating, so a check in __init__ would never run.
