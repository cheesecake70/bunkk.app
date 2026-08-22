"""Flask CLI commands — the cron surface of the app (Phase 3).

One hourly line in crontab covers everyone, because each run only picks up the
users whose chosen hour matches:

    0 * * * * cd /srv/bunkr && .venv/bin/flask push-briefs >> /var/log/bunkr.log 2>&1
"""
from __future__ import annotations

import json

import click
from flask import Flask


def register(app: Flask) -> None:
    @app.cli.command("push-briefs")
    @click.option("--hour", type=int, default=None,
                  help="Target this hour instead of the current one.")
    @click.option("--force", is_flag=True,
                  help="Ignore each user's schedule and notify everyone opted in.")
    @click.option("--dry-run", is_flag=True,
                  help="Show what would be sent without sending anything.")
    def push_briefs(hour, force, dry_run):
        """Send the morning brief to everyone due one."""
        from . import planning, push
        from .models import Settings, User
        from . import db

        if dry_run:
            users = (
                db.session.query(User)
                .join(Settings, Settings.user_id == User.id)
                .filter(Settings.notify_enabled.is_(True))
                .all()
            )
            preview = []
            for user in users:
                brief = planning.morning_brief(user)
                preview.append({
                    "user": user.email,
                    "brief": {"title": brief.title, "body": brief.body} if brief else None,
                })
            click.echo(json.dumps({"dry_run": True, "users": preview}, indent=1))
            return

        if not push.is_configured():
            raise click.ClickException(
                "VAPID keys aren't set — see app/push.py for how to generate them."
            )

        summary = push.send_morning_briefs(hour=hour, force=force)
        click.echo(json.dumps(summary, indent=1))

    @app.cli.command("vapid-keys")
    def vapid_keys():
        """Print a fresh VAPID key pair for the environment file."""
        from .push import generate_vapid_keys

        keys = generate_vapid_keys()
        click.echo("VAPID_PUBLIC_KEY=" + keys["public"])
        click.echo("VAPID_PRIVATE_KEY=" + keys["private"])
        click.echo("VAPID_CLAIM_EMAIL=mailto:you@example.com")
