"""Configuration objects. Select with BUNKMATE_CONFIG env var."""
import os

BASEDIR = os.path.abspath(os.path.dirname(__file__))


class BaseConfig:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_DIR = os.environ.get(
        "BUNKMATE_UPLOAD_DIR", os.path.join(BASEDIR, "instance", "uploads")
    )
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB — reports are ~25-400 KB

    # Web Push (Phase 3). Absent keys simply disable notifications; everything
    # else keeps working, so dev and self-hosted prod don't diverge.
    VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY")
    VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY")
    VAPID_CLAIM_EMAIL = os.environ.get("VAPID_CLAIM_EMAIL", "mailto:admin@example.com")


class DevConfig(BaseConfig):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(BASEDIR, "instance", "bunkmate.db")
    )


class TestConfig(BaseConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite://"  # in-memory


class ProdConfig(BaseConfig):
    DEBUG = False
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(BASEDIR, "instance", "bunkmate.db")
    )
    # Self-hosted deployment: gunicorn behind nginx/Caddy with HTTPS.
    SESSION_COOKIE_SECURE = True
