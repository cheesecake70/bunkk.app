"""Bunkmate Flask application factory."""
from __future__ import annotations

import os

import sqlite3

from flask import Flask
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()


@event.listens_for(Engine, "connect")
def _sqlite_pragmas(dbapi_connection, connection_record):
    """Make SQLite fit for several people at once (ADR-2).

    WAL lets readers carry on while one writer commits, which is exactly this
    workload: constant dashboard reads, occasional upload bursts. Without it a
    single upload blocks every other user's page. The busy timeout turns the
    remaining write contention into a short wait instead of an error, and
    foreign keys stop a deleted account leaving orphaned lectures behind.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_app(config_object: str | None = None) -> Flask:
    # Templates and static assets live at the repo root, beside the app package.
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder="../templates",
        static_folder="../static",
    )

    app.config.from_object(config_object or os.environ.get(
        "BUNKMATE_CONFIG", "config.DevConfig"
    ))
    os.makedirs(app.instance_path, exist_ok=True)

    # Forgeable sessions stop being a dev nicety the moment other students have
    # accounts on this box, so refuse to serve them.
    from config import DEV_SECRET_KEY

    if (
        app.config["SECRET_KEY"] == DEV_SECRET_KEY
        and not app.debug
        and not app.testing
    ):
        raise RuntimeError(
            "SECRET_KEY is still the development default. Generate one with "
            "`python -c \"import secrets; print(secrets.token_hex(32))\"` and "
            "set it in the environment before serving real users."
        )

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    from . import models  # noqa: F401  (register models with SQLAlchemy)

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(models.User, int(user_id))

    from . import cli, filters
    filters.register(app)
    cli.register(app)

    from .account import bp as account_bp
    from .api import bp as api_bp
    from .auth import bp as auth_bp
    from .routes import bp as core_bp

    app.register_blueprint(core_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(account_bp)

    return app
