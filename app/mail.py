"""Outgoing email — one function, the standard library, no new dependency.

Bunkr sends exactly one kind of message (a password-reset link), so a mail
extension would be more machinery than the job needs. What matters here is that
a provider having a bad day can never take a request down with it: every failure
is caught and logged, and the caller is told nothing, because the caller must
answer identically whether or not the address has an account.

With no MAIL_SERVER configured the body is logged instead of sent. That is the
development path — the reset link is right there in the console — and it is also
why `send_mail` returning False is not an error worth surfacing.
"""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from flask import current_app

#: Long enough to fail on a dead host, short enough that a request never hangs.
SMTP_TIMEOUT = 10

#: Where TESTING mode collects messages instead of sending them.
OUTBOX = "bunkr_outbox"


def send_mail(to: str, subject: str, body: str) -> bool:
    """Hand one plain-text message to SMTP. True when it was accepted."""
    sender = current_app.config.get("MAIL_DEFAULT_SENDER") or "bunkr@localhost"

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    if current_app.config.get("TESTING"):
        current_app.extensions.setdefault(OUTBOX, []).append(message)
        return True

    host = current_app.config.get("MAIL_SERVER")
    if not host:
        current_app.logger.info(
            "No MAIL_SERVER configured; message to %s not sent:\n%s", to, body
        )
        return False

    try:
        with smtplib.SMTP(host, current_app.config.get("MAIL_PORT", 587),
                          timeout=SMTP_TIMEOUT) as smtp:
            if current_app.config.get("MAIL_USE_TLS"):
                smtp.starttls()
            username = current_app.config.get("MAIL_USERNAME")
            if username:
                smtp.login(username, current_app.config.get("MAIL_PASSWORD") or "")
            smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        # Never re-raised: the view above this must answer the same way whether
        # the address exists, whether the provider is up, and whether the
        # credentials are right.
        current_app.logger.warning("Could not send mail to %s: %s", to, exc)
        return False
    return True
