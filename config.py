"""Configuration objects. Select with BUNKR_CONFIG env var."""
import os

BASEDIR = os.path.abspath(os.path.dirname(__file__))


#: Anyone who knows this can forge another user's session, so production
#: refuses to start with it.
DEV_SECRET_KEY = "dev-only-change-me"


class BaseConfig:
    SECRET_KEY = os.environ.get("SECRET_KEY", DEV_SECRET_KEY)
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_DIR = os.environ.get(
        "BUNKR_UPLOAD_DIR", os.path.join(BASEDIR, "instance", "uploads")
    )
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB — reports are ~25-400 KB


class DevConfig(BaseConfig):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(BASEDIR, "instance", "bunkr.db")
    )


class TestConfig(BaseConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite://"  # in-memory


class ProdConfig(BaseConfig):
    DEBUG = False
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///" + os.path.join(BASEDIR, "instance", "bunkr.db")
    )
    # Self-hosted deployment: gunicorn behind nginx/Caddy with HTTPS.
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # The boot-time guard lives in create_app: Flask's from_object reads class
    # attributes without instantiating, so a check in __init__ would never run.
