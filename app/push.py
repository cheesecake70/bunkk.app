"""Web Push — the morning nudge (Phase 3).

Push is optional infrastructure: if VAPID keys aren't configured the app is
fully usable, the UI just says notifications are unavailable. That keeps local
development and the self-hosted deployment from diverging.

Generate a key pair once and put both halves in the environment:

    flask vapid-keys
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from flask import current_app

from . import db
from .models import PushSubscription, Settings, User

log = logging.getLogger(__name__)

#: Endpoints answering with these are gone for good — drop them.
DEAD_STATUSES = (404, 410)


def generate_vapid_keys() -> dict[str, str]:
    """A fresh P-256 pair, base64url-encoded the way the Web Push spec wants.

    VAPID keys are EC P-256, so the public half is the uncompressed X9.62 point
    (65 bytes) and the private half is the raw scalar (32 bytes) — not the
    `*_bytes_raw()` form that only Ed25519/X25519 keys have.
    """
    import base64

    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01

    vapid = Vapid01()
    vapid.generate_keys()

    def encode(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    public_point = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    private_scalar = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")

    return {"public": encode(public_point), "private": encode(private_scalar)}


def is_configured() -> bool:
    return bool(
        current_app.config.get("VAPID_PUBLIC_KEY")
        and current_app.config.get("VAPID_PRIVATE_KEY")
    )


def public_key() -> str | None:
    return current_app.config.get("VAPID_PUBLIC_KEY")


def send(subscription: PushSubscription, payload: dict) -> bool:
    """Deliver one notification. Returns False if the subscription is dead.

    A dead subscription is deleted here rather than left to rot: browsers
    rotate endpoints on reinstall, and a stale row would fail forever.
    """
    if not is_configured():
        return False

    from pywebpush import WebPushException, webpush

    try:
        webpush(
            subscription_info=subscription.as_info(),
            data=json.dumps(payload),
            vapid_private_key=current_app.config["VAPID_PRIVATE_KEY"],
            vapid_claims={"sub": current_app.config["VAPID_CLAIM_EMAIL"]},
            timeout=10,
        )
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", None)
        if status in DEAD_STATUSES:
            log.info("Dropping dead push subscription %s (%s)", subscription.id, status)
            db.session.delete(subscription)
            db.session.commit()
            return False
        log.warning("Push to subscription %s failed: %s", subscription.id, exc)
        return False
    except Exception:                                   # transport/DNS/etc.
        log.exception("Push to subscription %s raised", subscription.id)
        return False

    subscription.last_sent_at = datetime.now(timezone.utc)
    db.session.commit()
    return True


def send_to_user(user: User, payload: dict) -> int:
    """Fan out to every device this user has installed. Returns delivery count."""
    subscriptions = (
        db.session.query(PushSubscription).filter_by(user_id=user.id).all()
    )
    return sum(1 for sub in subscriptions if send(sub, payload))


def brief_payload(brief) -> dict:
    return {
        "title": brief.title,
        "body": brief.body,
        "url": brief.url,
        "tag": brief.tag,
    }


def send_morning_briefs(hour: int | None = None, force: bool = False) -> dict:
    """Send each opted-in user their brief. Intended for an hourly cron.

    Passing an hour (or letting it default to now) means one cron line covers
    every user's chosen time without the job needing to know who they are.
    """
    from . import planning

    if hour is None:
        hour = datetime.now().hour

    summary = {"considered": 0, "sent": 0, "skipped_no_news": 0, "users": []}

    query = db.session.query(User).join(Settings, Settings.user_id == User.id)
    if not force:
        query = query.filter(Settings.notify_enabled.is_(True),
                             Settings.notify_hour == hour)

    for user in query.all():
        summary["considered"] += 1
        brief = planning.morning_brief(user)
        if brief is None:
            summary["skipped_no_news"] += 1
            continue
        delivered = send_to_user(user, brief_payload(brief))
        summary["sent"] += delivered
        summary["users"].append(
            {"user_id": user.id, "title": brief.title, "delivered": delivered}
        )

    return summary
