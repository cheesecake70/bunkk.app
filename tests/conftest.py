import sys
from pathlib import Path
from unittest.mock import patch

# Make the repo root importable (report_parser, app, config).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def google_sign_in(client, email="m@example.com", username=None, *, sub=None,
                   verified=True, headers=None, query=""):
    """Sign `client` in as `email`, standing in for Google.

    The app never talks to Google in tests: the callback's one call for the
    verified claims is replaced, and everything after it — matching or
    creating the account, the session cookie, the redirect — runs for real.
    `username` renames the account afterwards, since sign-up no longer asks.
    """
    claims = {"sub": sub or f"sub-{email}", "email": email,
              "email_verified": verified, "name": email.split("@")[0]}
    with patch("app.auth._google_userinfo", return_value=claims):
        resp = client.get("/auth/google/callback?code=x&state=y" + query,
                          headers=headers or {})
    if username and resp.status_code == 302:
        from app import db
        from app.models import User
        with client.application.app_context():
            user = db.session.query(User).filter_by(email=email).one()
            user.username = username
            db.session.commit()
    return resp
