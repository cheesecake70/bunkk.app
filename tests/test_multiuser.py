"""Multiple accounts on one Bunkk.

Isolation was asserted from Phase 1, so the new ground here is everything that
only becomes a question with a second person: who may create an account, whose
identity a report claims, what a shared device does, and whether leaving takes
your data with you.
"""
from pathlib import Path

import pytest
from conftest import google_sign_in

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


def register(client, email, username=None):
    return google_sign_in(client, email, username)


def upload(client, path=GOLDEN):
    return client.post(
        "/api/reports",
        data={"report": (path.open("rb"), "report.pdf")},
        content_type="multipart/form-data",
    )


class TestRegistration:
    """There is no sign-up form: the first Google sign-in creates the account."""

    def test_signing_in_creates_the_account(self, app):
        client = app.test_client()
        resp = register(client, "aditi@example.com")
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/upload")
        with app.app_context():
            user = db.session.query(User).one()
            assert (user.email, user.username) == ("aditi@example.com", "aditi")
            assert user.google_sub == "sub-aditi@example.com"
            assert user.is_verified

    def test_the_second_sign_in_finds_the_same_account(self, app):
        client = app.test_client()
        register(client, "aditi@example.com")
        client.post("/logout")
        resp = register(client, "aditi@example.com")
        assert resp.headers["Location"].endswith("/")
        with app.app_context():
            assert db.session.query(User).count() == 1

    def test_accounts_are_matched_on_google_s_id_not_the_address(self, app):
        """An address can be renamed at Google; the `sub` never changes."""
        client = app.test_client()
        google_sign_in(client, "old@example.com", sub="sub-42")
        client.post("/logout")
        google_sign_in(client, "new@example.com", sub="sub-42")
        with app.app_context():
            user = db.session.query(User).one()
            assert user.email == "new@example.com"

    def test_a_pre_google_account_is_adopted_by_its_address(self, app):
        """Everyone registered before Google sign-in keeps their ledger."""
        with app.app_context():
            db.session.add(User(email="old@example.com", username="oldie"))
            db.session.commit()
        client = app.test_client()
        resp = google_sign_in(client, "old@example.com", sub="sub-99")
        assert resp.headers["Location"].endswith("/")
        with app.app_context():
            user = db.session.query(User).one()
            assert (user.username, user.google_sub) == ("oldie", "sub-99")

    def test_suggested_usernames_never_collide(self, app):
        for email in ("aditi@one.com", "aditi@two.com", "aditi@three.com"):
            register(app.test_client(), email)
        with app.app_context():
            names = sorted(u.username for u in db.session.query(User).all())
            assert names == ["aditi", "aditi2", "aditi3"]

    def test_username_is_validated_on_the_profile_form(self, app):
        client = app.test_client()
        register(client, "x@example.com")
        for bad in ("ab", "no spaces", "way" + "y" * 40, "bad/slash"):
            client.post("/account/profile", data={"username": bad})
            with app.app_context():
                assert db.session.query(User).one().username == "x00", f"{bad!r} should be refused"

    def test_usernames_are_unique_case_insensitively(self, app):
        register(app.test_client(), "one@example.com", username="Aditi")
        second = app.test_client()
        register(second, "two@example.com")
        second.post("/account/profile", data={"username": "aditi"})
        with app.app_context():
            names = {u.username for u in db.session.query(User).all()}
            assert names == {"Aditi", "two"}

    def test_no_invite_code_is_needed(self, app):
        """Registration is open — several accounts, no codes anywhere."""
        for i in range(3):
            client = app.test_client()
            assert register(client, f"student{i}@example.com").status_code == 302
        with app.app_context():
            assert db.session.query(User).count() == 3


class TestLoginRedirects:
    def test_login_will_not_bounce_you_off_site(self, app):
        from unittest.mock import patch
        client = app.test_client()
        with patch("authlib.integrations.flask_client.apps.FlaskOAuth2App.authorize_redirect",
                   return_value=("", 302, {"Location": "https://accounts.google.com/o/x"})):
            client.get("/login/google?next=https://evil.example.com/steal")
        resp = register(client, "m@example.com")
        assert resp.headers["Location"].endswith("/upload")
        client.post("/logout")
        with patch("authlib.integrations.flask_client.apps.FlaskOAuth2App.authorize_redirect",
                   return_value=("", 302, {"Location": "https://accounts.google.com/o/x"})):
            client.get("/login/google?next=https://evil.example.com/steal")
        resp = register(client, "m@example.com")
        assert resp.headers["Location"] == "/"

    def test_next_survives_the_trip_to_google(self, app):
        from unittest.mock import patch
        client = app.test_client()
        register(client, "m@example.com")
        client.post("/logout")
        with patch("authlib.integrations.flask_client.apps.FlaskOAuth2App.authorize_redirect",
                   return_value=("", 302, {"Location": "https://accounts.google.com/o/x"})):
            client.get("/login/google?next=/settings")
        resp = register(client, "m@example.com")
        assert resp.headers["Location"] == "/settings"

    def test_the_hint_reaches_google(self, app):
        from unittest.mock import patch
        client = app.test_client()
        with patch("authlib.integrations.flask_client.apps.FlaskOAuth2App.authorize_redirect",
                   return_value=("", 302, {"Location": "https://accounts.google.com/o/x"})) as go:
            client.get("/login/google?as=owner@example.com")
        assert go.call_args.kwargs["login_hint"] == "owner@example.com"
        assert go.call_args.args[0].endswith("/auth/google/callback")


class TestIdentityClaiming:
    def test_one_student_may_have_several_accounts(self, app):
        """Starting a fresh account and uploading your own report into it is
        an ordinary thing to do. Each account gets a ledger of its own."""
        first = app.test_client()
        register(first, "old@example.com")
        assert upload(first).status_code == 200

        second = app.test_client()
        register(second, "new@example.com")
        resp = upload(second)

        assert resp.status_code == 200
        assert resp.get_json()["status"] == "merged"
        assert "claimed_by" not in resp.get_json()
        with app.app_context():
            users = {u.email: u.student_number for u in db.session.query(User).all()}
            assert users == {"old@example.com": "60000000001",
                             "new@example.com": "60000000001"}
            for user in db.session.query(User).all():
                assert db.session.query(LectureInstance).filter_by(
                    user_id=user.id).count() == 126

    def test_the_two_ledgers_stay_separate(self, app):
        first = app.test_client()
        register(first, "old@example.com")
        upload(first)
        second = app.test_client()
        register(second, "new@example.com")
        upload(second)

        # A guess in one account moves nothing in the other.
        before = first.get("/api/dashboard").get_json()["overall"]
        assert second.post("/api/predictions/bulk",
                           json={"predicted": "P"}).get_json()["changed"] > 0
        assert first.get("/api/dashboard").get_json()["overall"] == before

    def test_an_account_still_holds_only_one_student(self, app, tmp_path):
        from datetime import date, time
        from reportlab_stub import make_detailed_pdf

        client = app.test_client()
        register(client, "m@example.com")
        assert upload(client).status_code == 200

        other = tmp_path / "other.pdf"
        make_detailed_pdf(
            other,
            [("Computer NetworksT C2", date(2026, 7, 16), time(10, 0, 1), time(11, 0, 0), "P")],
            student_number="60004250099", roll_no="C102", student_name="OTHER STUDENT",
        )
        resp = upload(client, other)
        assert resp.status_code == 422
        assert "belongs to student 60004250099" in resp.get_json()["error"]

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
            assert users == {"a@example.com": "60000000001", "b@example.com": "60004250099"}
            assert db.session.query(LectureInstance).filter_by(user_id=1).count() == 126
            assert db.session.query(LectureInstance).filter_by(user_id=2).count() == 1




class TestDeletion:
    def test_deleting_removes_everything_and_leaves_others_alone(self, app):
        keeper = app.test_client()
        register(keeper, "keeper@example.com")
        upload(keeper)

        leaver = app.test_client()
        register(leaver, "leaver@example.com")

        resp = leaver.post("/account/delete", data={"confirm": "leaver"},
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

        client.post("/account/delete", data={"confirm": "m00"})

        with app.app_context():
            assert db.session.query(User).count() == 0
            for model in (LectureInstance, Subject, Semester, ReportSnapshot, Settings):
                assert db.session.query(model).count() == 0
        assert not os.path.exists(upload_dir)

    def test_a_wrong_confirmation_deletes_nothing(self, app):
        client = app.test_client()
        register(client, "m@example.com")
        upload(client)

        client.post("/account/delete", data={"confirm": "not-my-username"})
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
        client.post("/account/delete", data={"confirm": "m00"})

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


class TestSessions:
    """What the session cookie carries, and what invalidates it.

    SQLite hands a deleted row's id to the next account created. With the id in
    the cookie, a browser left signed in as a deleted account was signed in as
    whoever took that id next — reproduced by hand before this was written.
    """

    def test_the_cookie_carries_a_token_not_the_row_id(self, app):
        client = app.test_client()
        register(client, "m@example.com")
        with app.app_context():
            user = db.session.query(User).one()
            assert user.get_id() == user.session_token
            assert not user.get_id().isdigit()

    def test_a_stale_cookie_cannot_reach_a_reused_id(self, app):
        first = app.test_client()
        register(first, "gone@example.com")
        with app.app_context():
            original_id = db.session.query(User).one().id
        first.post("/account/delete", data={"confirm": "gone"})

        second = app.test_client()
        register(second, "new@example.com")
        with app.app_context():
            reborn = db.session.query(User).one()
            assert reborn.id == original_id, "expected SQLite to reuse the id"

        # The deleted account's browser: its cookie is for a token nobody holds.
        stale = app.test_client()
        stale.set_cookie("session", first.get_cookie("session").value)
        assert stale.get("/settings").status_code == 302

    def test_signing_out_other_devices_keeps_this_one(self, app):
        here = app.test_client()
        register(here, "m@example.com")

        elsewhere = app.test_client()
        register(elsewhere, "m@example.com")
        assert elsewhere.get("/").status_code == 200

        resp = here.post("/account/sessions/revoke")
        assert resp.status_code == 302
        assert here.get("/").status_code == 200          # this browser stays
        assert elsewhere.get("/settings").status_code == 302     # that one doesn't


class TestSigningOutTowardsAnotherAccount:
    """Sign-out can carry where to land, so switching accounts arrives at the
    login page with the right one already named."""

    def test_signing_out_can_land_on_that_account_s_login(self, app):
        other = app.test_client()
        register(other, "second@example.com", username="second")
        resp = other.post("/logout", data={"next": "/login?as=owner"})
        assert resp.headers["Location"] == "/login?as=owner"

        page = other.get("/login?as=owner").get_data(as_text=True)
        assert "<strong>owner</strong>" in page
        assert "/login/google?as=owner" in page

    def test_sign_out_still_refuses_to_leave_the_site(self, app):
        client = app.test_client()
        register(client, "m@example.com")
        resp = client.post("/logout", data={"next": "https://evil.example.com/x"})
        assert resp.headers["Location"] == "/login"

    def test_an_upload_failure_names_nobody(self, app):
        """No refusal says anything about any other account."""
        client = app.test_client()
        register(client, "m@example.com")
        summary = Path(__file__).parent / "golden" / "summary_july.pdf"
        resp = upload(client, summary)
        assert resp.status_code == 422
        assert "claimed_by" not in resp.get_json()
