"""Multiple accounts on one Bunkr.

Isolation was asserted from Phase 1, so the new ground here is everything that
only becomes a question with a second person: who may create an account, whose
identity a report claims, what a shared device does, and whether leaving takes
your data with you.
"""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import create_app, db
from app.models import (
    LectureInstance,
    ReportSnapshot,
    Semester,
    Settings,
    Subject,
    User,
)

GOLDEN = Path(__file__).parent / "golden" / "detailed_jul_aug.pdf"


def make_app(tmp_path, **config):
    app = create_app("config.TestConfig")
    app.config.update(UPLOAD_DIR=str(tmp_path / "uploads"), **config)
    with app.app_context():
        db.create_all()
    return app


@pytest.fixture()
def app(tmp_path):
    app = make_app(tmp_path)
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def register(client, email, password="password123", username=None):
    handle = username or re.sub(r"[^A-Za-z0-9_.]", "", email.split("@")[0]).ljust(3, "x")
    return client.post("/register", data={
        "email": email, "username": handle,
        "password": password, "confirm_password": password,
    })


def upload(client, path=GOLDEN):
    return client.post(
        "/api/reports",
        data={"report": (path.open("rb"), "report.pdf")},
        content_type="multipart/form-data",
    )


class TestRegistration:
    """Open registration: email, username, and a password typed twice."""

    def test_signing_up_creates_the_account(self, app):
        client = app.test_client()
        assert register(client, "aditi@example.com").status_code == 302
        with app.app_context():
            user = db.session.query(User).one()
            assert (user.email, user.username) == ("aditi@example.com", "aditi")

    def test_password_must_be_confirmed(self, app):
        client = app.test_client()
        resp = client.post("/register", data={
            "email": "a@example.com", "username": "aditi",
            "password": "password123", "confirm_password": "password124",
        })
        assert resp.status_code == 400
        assert "match" in resp.get_data(as_text=True)
        with app.app_context():
            assert db.session.query(User).count() == 0

    def test_username_is_required_and_validated(self, app):
        client = app.test_client()
        for bad in ("", "ab", "no spaces", "way" + "y" * 40, "bad/slash"):
            resp = client.post("/register", data={
                "email": f"x{len(bad)}@example.com", "username": bad,
                "password": "password123", "confirm_password": "password123",
            })
            assert resp.status_code == 400, f"{bad!r} should be refused"
        with app.app_context():
            assert db.session.query(User).count() == 0

    def test_usernames_are_unique_case_insensitively(self, app):
        first = app.test_client()
        register(first, "one@example.com", username="Aditi")

        second = app.test_client()
        resp = second.post("/register", data={
            "email": "two@example.com", "username": "aditi",
            "password": "password123", "confirm_password": "password123",
        })
        assert resp.status_code == 400
        assert "taken" in resp.get_data(as_text=True)

    def test_you_can_sign_in_with_the_username(self, app):
        client = app.test_client()
        register(client, "aditi@example.com", username="aditi")
        client.post("/logout")

        resp = client.post("/login", data={"email": "aditi", "password": "password123"})
        assert resp.status_code == 302
        assert client.get("/").status_code == 200

    def test_no_invite_code_is_needed(self, app):
        """Registration is open — several accounts, no codes anywhere."""
        for i in range(3):
            client = app.test_client()
            assert register(client, f"student{i}@example.com").status_code == 302
        with app.app_context():
            assert db.session.query(User).count() == 3


class TestLoginLockout:
    def test_repeated_failures_lock_the_account(self, app):
        from app.auth import MAX_FAILED_LOGINS

        client = app.test_client()
        register(client, "m@example.com")
        client.post("/logout")

        for _ in range(MAX_FAILED_LOGINS):
            client.post("/login", data={"email": "m@example.com", "password": "wrong-one"})

        resp = client.post("/login", data={"email": "m@example.com", "password": "password123"})
        assert resp.status_code == 429
        assert "Too many attempts" in resp.get_data(as_text=True)

    def test_a_good_password_resets_the_counter(self, app):
        client = app.test_client()
        register(client, "m@example.com")
        client.post("/logout")

        client.post("/login", data={"email": "m@example.com", "password": "wrong-one"})
        client.post("/login", data={"email": "m@example.com", "password": "password123"})

        with app.app_context():
            assert db.session.query(User).one().failed_logins == 0

    def test_the_lock_expires(self, app):
        from app.auth import MAX_FAILED_LOGINS

        client = app.test_client()
        register(client, "m@example.com")
        client.post("/logout")
        for _ in range(MAX_FAILED_LOGINS):
            client.post("/login", data={"email": "m@example.com", "password": "wrong-one"})

        with app.app_context():
            user = db.session.query(User).one()
            user.locked_until = datetime.now(timezone.utc) - timedelta(minutes=1)
            db.session.commit()

        resp = client.post("/login", data={"email": "m@example.com", "password": "password123"})
        assert resp.status_code == 302

    def test_unknown_emails_look_identical_to_wrong_passwords(self, app):
        """Otherwise the login form becomes a way to enumerate who has an account."""
        client = app.test_client()
        register(client, "m@example.com")
        client.post("/logout")

        real = client.post("/login", data={"email": "m@example.com", "password": "nope1234"})
        fake = client.post("/login", data={"email": "ghost@example.com", "password": "nope1234"})

        assert real.status_code == fake.status_code == 401
        # Substring avoids the apostrophe, which Jinja escapes to &#39;.
        message = "match an account"
        assert message in real.get_data(as_text=True)
        assert message in fake.get_data(as_text=True)

    def test_login_will_not_bounce_you_off_site(self, app):
        client = app.test_client()
        register(client, "m@example.com")
        client.post("/logout")

        resp = client.post("/login?next=https://evil.example.com/steal",
                           data={"email": "m@example.com", "password": "password123", "username": "muser", "confirm_password": "password123"})
        assert resp.headers["Location"] == "/"


class TestIdentityClaiming:
    def test_a_report_belongs_to_one_account_only(self, app):
        """Uploading a friend's PDF must not silently claim their identity."""
        first = app.test_client()
        register(first, "real@example.com")
        assert upload(first).status_code == 200

        second = app.test_client()
        register(second, "impostor@example.com")
        resp = upload(second)

        assert resp.status_code == 422
        assert "already set up on another" in resp.get_json()["error"]
        with app.app_context():
            assert db.session.query(LectureInstance).count() == 126   # only one ledger

    def test_the_owner_can_still_re_upload(self, app):
        client = app.test_client()
        register(client, "real@example.com")
        upload(client)
        assert upload(client).get_json()["status"] == "duplicate"

    def test_two_students_keep_separate_ledgers(self, app, tmp_path):
        """Different student numbers coexist happily."""
        import sys

        sys.path.insert(0, str(Path(__file__).parent))
        from datetime import date, time

        from reportlab_stub import make_detailed_pdf

        other = tmp_path / "other.pdf"
        make_detailed_pdf(
            other,
            [("Computer NetworksT C2", date(2026, 7, 16), time(10, 0, 1), time(11, 0, 0), "P")],
            student_number="60004250099", roll_no="C102", student_name="OTHER STUDENT",
        )

        first = app.test_client()
        register(first, "a@example.com")
        upload(first)

        second = app.test_client()
        register(second, "b@example.com")
        assert upload(second, other).status_code == 200

        with app.app_context():
            users = {u.email: u.student_number for u in db.session.query(User).all()}
            assert users == {"a@example.com": "60004250098", "b@example.com": "60004250099"}
            assert db.session.query(LectureInstance).filter_by(user_id=1).count() == 126
            assert db.session.query(LectureInstance).filter_by(user_id=2).count() == 1




class TestDeletion:
    def test_deleting_removes_everything_and_leaves_others_alone(self, app):
        keeper = app.test_client()
        register(keeper, "keeper@example.com")
        upload(keeper)

        leaver = app.test_client()
        register(leaver, "leaver@example.com")

        resp = leaver.post("/account/delete", data={"password": "password123"},
                           follow_redirects=True)
        assert resp.status_code == 200

        with app.app_context():
            emails = {u.email for u in db.session.query(User).all()}
            assert emails == {"keeper@example.com"}
            assert db.session.query(Settings).count() == 1
            # The remaining account is untouched.
            assert db.session.query(LectureInstance).count() == 126

    def test_deletion_takes_the_ledger_and_the_pdfs(self, app, tmp_path):
        import os

        client = app.test_client()
        register(client, "m@example.com")
        upload(client)

        upload_dir = os.path.join(str(tmp_path / "uploads"), "1")
        assert os.path.isdir(upload_dir)

        client.post("/account/delete", data={"password": "password123"})

        with app.app_context():
            assert db.session.query(User).count() == 0
            for model in (LectureInstance, Subject, Semester, ReportSnapshot, Settings):
                assert db.session.query(model).count() == 0
        assert not os.path.exists(upload_dir)

    def test_a_wrong_password_deletes_nothing(self, app):
        client = app.test_client()
        register(client, "m@example.com")
        upload(client)

        client.post("/account/delete", data={"password": "not-my-password"})
        with app.app_context():
            assert db.session.query(User).count() == 1
            assert db.session.query(LectureInstance).count() == 126

    def test_deletion_leaves_no_orphans_behind(self, app):
        """Foreign keys are enforced now, so a bad delete order would raise —
        this asserts the whole graph actually goes."""
        import sqlalchemy as sa

        client = app.test_client()
        register(client, "m@example.com")
        upload(client)
        client.post("/account/delete", data={"password": "password123"})

        with app.app_context():
            violations = db.session.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
            assert violations == []


class TestSettingsIsOnePage:
    def test_account_url_redirects_into_settings(self, app):
        client = app.test_client()
        register(client, "m@example.com")
        resp = client.get("/account/")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/settings")

    def test_settings_holds_profile_limits_and_deletion(self, app):
        client = app.test_client()
        register(client, "m@example.com")
        html = client.get("/settings").get_data(as_text=True)

        for section in ("Profile", "Attendance limits", "Delete this account"):
            assert section in html, f"{section} missing from Settings"
        assert "Take your data" not in html          # export is gone
        assert "Invite" not in html                  # so are invites
        assert "notification" not in html.lower()    # and so are notifications

    def test_username_can_be_changed_from_settings(self, app):
        client = app.test_client()
        register(client, "m@example.com", username="before")
        client.post("/account/profile", data={"username": "after"})

        with app.app_context():
            assert db.session.query(User).one().username == "after"

    def test_there_is_no_display_name_to_set(self, app):
        """One name per account. A second one nothing ever rendered was a field
        to keep in step with nothing on the other end of it."""
        client = app.test_client()
        register(client, "m@example.com", username="solo")

        html = client.get("/settings").get_data(as_text=True)
        assert "Display name" not in html

        client.post("/account/profile", data={"username": "solo", "name": "Mokssha"})
        with app.app_context():
            assert not hasattr(db.session.query(User).one(), "name")

    def test_a_taken_username_is_refused_on_edit(self, app):
        first = app.test_client()
        register(first, "one@example.com", username="taken")
        second = app.test_client()
        register(second, "two@example.com", username="mine")

        second.post("/account/profile", data={"username": "taken"})
        with app.app_context():
            user = db.session.query(User).filter_by(email="two@example.com").one()
            assert user.username == "mine"


class TestIsolationAcrossPhase2And3:
    def test_a_second_user_sees_none_of_the_first_users_planning(self, app):
        first = app.test_client()
        register(first, "a@example.com")
        upload(first)

        second = app.test_client()
        register(second, "b@example.com")

        assert second.get("/timetable").status_code == 200
        assert second.post("/api/simulate", json={"absences": []}).get_json()["subjects"] == []
        with app.app_context():
            assert db.session.query(Subject).filter_by(user_id=2).count() == 0

    def test_account_pages_need_a_session(self, app):
        anon = app.test_client()
        assert anon.get("/account/").status_code == 302
        assert anon.get("/settings").status_code == 302
        assert anon.post("/account/delete", data={}).status_code == 302
        assert anon.post("/account/profile", data={}).status_code == 302
