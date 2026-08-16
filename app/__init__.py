"""Bunkmate Flask application factory."""
from __future__ import annotations

import os

from flask import Flask
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()


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

    from .api import bp as api_bp
    from .auth import bp as auth_bp
    from .routes import bp as core_bp

    app.register_blueprint(core_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)

    return app
