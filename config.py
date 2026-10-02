"""Configuration objects. Select with BUNKK_CONFIG env var.

A `.env` file beside this module is loaded first, so a deployment only ever
has to fill in `.env.example`. Real environment variables win over the file.
"""
import os

from dotenv import load_dotenv

BASEDIR = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(BASEDIR, ".env"))


#: Anyone who knows this can forge another user's session, so production
#: refuses to start with it.
DEV_SECRET_KEY = "dev-only-change-me"


def _bool(name: str, default: str) -> bool:
    return os.environ.get(name, default) not in ("0", "false", "False", "")


class BaseConfig:
    SECRET_KEY = os.environ.get("SECRET_KEY", DEV_SECRET_KEY)
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_DIR = os.environ.get(
        "BUNKK_UPLOAD_DIR", os.path.join(BASEDIR, "instance", "uploads")
    )
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB — reports are ~25-400 KB

    # CSRF tokens are bound to the session, not the clock: a page left open
    # over lunch must still be able to submit.
    WTF_CSRF_TIME_LIMIT = None

    # Per-IP throttles on the three routes that take a guess at a credential.
    # memory:// is per process; point this at Redis when running several
    # gunicorn workers, otherwise every worker gets its own allowance.
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_HEADERS_ENABLED = True

    # Google sign-in (the only way in). Created in Google Cloud Console; see
    # README "Google sign-in". Missing in dev, the login button 404s at Google
    # with a clear message rather than the app refusing to boot.
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
    #: /login/dev — sign in as any address without Google. Only honoured in
    #: DEBUG (see auth.dev_login); a laptop without an OAuth client needs it.
    DEV_LOGIN = _bool("BUNKK_DEV_LOGIN", "0")

    #: Environment variables `create_app` insists on. Empty outside production.
    REQUIRED_ENV: tuple[str, ...] = ()
    #: Trust X-Forwarded-* from one hop (nginx / Caddy in front of gunicorn).
    BEHIND_PROXY = False


class DevConfig(BaseConfig):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(BASEDIR, "instance", "bunkk.db")
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
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL", "")
    # Self-hosted deployment: gunicorn behind nginx/Caddy with HTTPS.
    BEHIND_PROXY = _bool("BUNKK_BEHIND_PROXY", "1")
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
        h.strip() for h in os.environ.get("BUNKK_TRUSTED_HOSTS", "").split(",")
        if h.strip()
    ] or None
    REQUIRED_ENV = (
        "SECRET_KEY", "DATABASE_URL", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
        "BUNKK_TRUSTED_HOSTS",
    )
    # The boot-time guard lives in create_app: Flask's from_object reads class
    # attributes without instantiating, so a check in __init__ would never run.
