"""Forgotten passwords.

Two things are being protected here at once, and they pull in opposite
directions: someone who has genuinely lost their password has to get back in,
and this page must not become a way to ask the server which addresses have
accounts. That is why every assertion about the response is an assertion that it
looks *identical* either way, and why the interesting checks are on the outbox.
"""
import re

import pytest

from app import create_app, db
from app.auth import make_reset_token
from app.mail import OUTBOX
from app.models import User


@pytest.fixture()
def app(tmp_path):
    app = create_app("config.TestConfig")
    app.config.update(UPLOAD_DIR=str(tmp_path / "uploads"))
    with app.app_context():
        db.create_all()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture(autouse=True)
def _no_cooldown(monkeypatch):
    """The rate limit is per process, so it would leak between tests."""
    import app.auth as auth_module
    monkeypatch.setattr(auth_module, "_reset_requests", {})


def register(client, email="m@example.com", password="password123"):
    return client.post("/register", data={
        "email": email, "username": "muser",
        "password": password, "confirm_password": password,
    })


def outbox(app):
    return app.extensions.get(OUTBOX, [])


def link_from(app):
    body = outbox(app)[-1].get_content()
    match = re.search(r"/reset/(\S+)", body)
    assert match, f"no reset link in:\n{body}"
    return match.group(0).rstrip(".")


class TestAsking:
    def test_a_known_and_an_unknown_address_answer_identically(self, app):
        client = app.test_client()
        register(client)
        client.post("/logout")

        known = client.post("/forgot", data={"email": "m@example.com"})
        unknown = client.post("/forgot", data={"email": "ghost@example.com"})

        assert known.status_code == unknown.status_code == 302
        assert known.headers["Location"] == unknown.headers["Location"]
        # Only one of them actually produced mail.
        assert len(outbox(app)) == 1

    def test_the_link_is_mailed_to_the_address_on_the_account(self, app):
        client = app.test_client()
        register(client, "m@example.com")
        client.post("/logout")

        client.post("/forgot", data={"email": "m@example.com"})
        assert outbox(app)[-1]["To"] == "m@example.com"
        assert "/reset/" in outbox(app)[-1].get_content()

    def test_one_address_cannot_be_used_to_send_mail_repeatedly(self, app):
        client = app.test_client()
        register(client)
        client.post("/logout")

        for _ in range(4):
            client.post("/forgot", data={"email": "m@example.com"})
        assert len(outbox(app)) == 1

    def test_signed_in_people_are_sent_to_settings(self, app):
        client = app.test_client()
        register(client)
        assert client.get("/forgot").headers["Location"] == "/settings"


class TestResetting:
    def test_the_link_sets_a_new_password_and_signs_you_in(self, app):
        client = app.test_client()
        register(client)
        client.post("/logout")
        client.post("/forgot", data={"email": "m@example.com"})

        link = link_from(app)
        assert client.get(link).status_code == 200
        resp = client.post(link, data={"password": "brand-new-pass",
                                       "confirm_password": "brand-new-pass"})
        assert resp.status_code == 302
        assert client.get("/").status_code == 200          # already signed in

        client.post("/logout")
        assert client.post("/login", data={"email": "m@example.com",
                                           "password": "brand-new-pass"}).status_code == 302

    def test_a_used_link_is_dead(self, app):
        """The token carries a piece of the password hash, so the first
        successful reset invalidates the mail it arrived in — and every copy of
        it, including a forwarded one."""
        client = app.test_client()
        register(client)
        client.post("/logout")
        client.post("/forgot", data={"email": "m@example.com"})
        link = link_from(app)

        client.post(link, data={"password": "brand-new-pass",
                                "confirm_password": "brand-new-pass"})
        again = client.get(link)
        assert again.status_code == 400
        assert "expired" in again.get_data(as_text=True)

    def test_changing_the_password_elsewhere_kills_an_outstanding_link(self, app):
        client = app.test_client()
        register(client)
        client.post("/forgot", data={"email": "m@example.com"})  # while signed in
        client.post("/logout")

        with app.app_context():
            user = db.session.query(User).one()
            link = f"/reset/{make_reset_token(user)}"

        client.post("/login", data={"email": "m@example.com", "password": "password123"})
        client.post("/account/password", data={
            "current_password": "password123",
            "new_password": "another-pass-1", "confirm_password": "another-pass-1",
        })
        assert client.get(link).status_code == 400

    def test_a_tampered_token_is_refused(self, app):
        client = app.test_client()
        register(client)
        client.post("/logout")
        assert client.get("/reset/not-a-real-token").status_code == 400

    def test_an_expired_link_is_refused(self, app, monkeypatch):
        import app.auth as auth_module

        client = app.test_client()
        register(client)
        client.post("/logout")
        client.post("/forgot", data={"email": "m@example.com"})
        link = link_from(app)

        monkeypatch.setattr(auth_module, "RESET_MAX_AGE", -1)
        assert client.get(link).status_code == 400

    def test_a_weak_new_password_is_refused_and_the_link_survives(self, app):
        client = app.test_client()
        register(client)
        client.post("/logout")
        client.post("/forgot", data={"email": "m@example.com"})
        link = link_from(app)

        assert client.post(link, data={"password": "short",
                                       "confirm_password": "short"}).status_code == 400
        assert client.get(link).status_code == 200      # still usable

    def test_resetting_signs_other_devices_out(self, app):
        elsewhere = app.test_client()
        register(elsewhere)

        here = app.test_client()
        here.post("/forgot", data={"email": "m@example.com"})
        with app.app_context():
            link = f"/reset/{make_reset_token(db.session.query(User).one())}"
        here.post(link, data={"password": "brand-new-pass",
                              "confirm_password": "brand-new-pass"})

        assert elsewhere.get("/").status_code == 302

    def test_the_lockout_is_cleared_so_you_can_use_the_new_password(self, app):
        from app.auth import MAX_FAILED_LOGINS

        client = app.test_client()
        register(client)
        client.post("/logout")
        for _ in range(MAX_FAILED_LOGINS):
            client.post("/login", data={"email": "m@example.com", "password": "nope1234"})

        with app.app_context():
            link = f"/reset/{make_reset_token(db.session.query(User).one())}"
        client.post(link, data={"password": "brand-new-pass",
                                "confirm_password": "brand-new-pass"})
        client.post("/logout")

        assert client.post("/login", data={"email": "m@example.com",
                                           "password": "brand-new-pass"}).status_code == 302


class TestDelivery:
    def test_without_a_mail_server_the_link_is_logged(self, app, caplog):
        """The development path: no SMTP anywhere, and the link is in the
        console rather than silently lost."""
        app.config.update(TESTING=False, MAIL_SERVER=None)
        client = app.test_client()
        register(client)
        client.post("/logout")

        with caplog.at_level("INFO"):
            client.post("/forgot", data={"email": "m@example.com"})
        assert "/reset/" in caplog.text

    def test_a_dead_mail_server_does_not_break_the_page(self, app):
        """A provider having a bad day must not turn into a 500 — and must not
        change the answer, which would leak whether the address exists."""
        app.config.update(TESTING=False, MAIL_SERVER="127.0.0.1", MAIL_PORT=1)
        client = app.test_client()
        register(client)
        client.post("/logout")

        resp = client.post("/forgot", data={"email": "m@example.com"})
        assert resp.status_code == 302
