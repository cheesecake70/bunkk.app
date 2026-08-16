"""Multiple accounts on one Bunkmate.

Isolation was asserted from Phase 1, so the new ground here is everything that
only becomes a question with a second person: who may create an account, whose
identity a report claims, what a shared device does, and whether leaving takes
your data with you.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import create_app, db
from app.models import (
    Invite,
    LectureInstance,
    PushSubscription,
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


@pytest.fixture()
def invite_app(tmp_path):
    app = make_app(tmp_path, REGISTRATION="invite")
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def register(client, email, password="password123", invite=None):
    data = {"email": email, "password": password}
    if invite:
        data["invite"] = invite
    return client.post("/register", data=data)


def upload(client, path=GOLDEN):
    return client.post(
        "/api/reports",
        data={"report": (path.open("rb"), "report.pdf")},
        content_type="multipart/form-data",
    )


class TestRegistrationGating:
    def test_first_account_needs_no_invite(self, invite_app):
        client = invite_app.test_client()
        assert register(client, "owner@example.com").status_code == 302
        with invite_app.app_context():
            assert db.session.query(User).count() == 1

    def test_second_account_is_refused_without_a_code(self, invite_app):
        first = invite_app.test_client()
        register(first, "owner@example.com")

        second = invite_app.test_client()
        resp = register(second, "friend@example.com")

        assert resp.status_code == 400
        assert "invite code" in resp.get_data(as_text=True).lower()
        with invite_app.app_context():
            assert db.session.query(User).count() == 1

    def test_a_valid_code_lets_a_friend_in_and_is_then_spent(self, invite_app):
        owner = invite_app.test_client()
        register(owner, "owner@example.com")
        owner.post("/account/invites", data={"note": "Aditi"})

        with invite_app.app_context():
            code = db.session.query(Invite).one().code

        friend = invite_app.test_client()
        assert register(friend, "friend@example.com", invite=code).status_code == 302

        with invite_app.app_context():
            invite = db.session.query(Invite).one()
            assert invite.is_used
            assert invite.used_by_id == 2

        # The same code cannot be reused.
        third = invite_app.test_client()
        assert register(third, "third@example.com", invite=code).status_code == 400
        with invite_app.app_context():
            assert db.session.query(User).count() == 2

    def test_revoking_an_unused_invite_kills_it(self, invite_app):
        owner = invite_app.test_client()
        register(owner, "owner@example.com")
        owner.post("/account/invites", data={})
        with invite_app.app_context():
            invite = db.session.query(Invite).one()
            code, invite_id = invite.code, invite.id

        owner.post(f"/account/invites/{invite_id}/revoke")
        friend = invite_app.test_client()
        assert register(friend, "friend@example.com", invite=code).status_code == 400

    def test_you_cannot_revoke_someone_elses_invite(self, invite_app):
        owner = invite_app.test_client()
        register(owner, "owner@example.com")
        owner.post("/account/invites", data={})
        with invite_app.app_context():
            invite_id = db.session.query(Invite).one().id
            code = db.session.query(Invite).one().code

        friend = invite_app.test_client()
        register(friend, "friend@example.com", invite=code)
        friend.post(f"/account/invites/{invite_id}/revoke")

        with invite_app.app_context():
            assert db.session.query(Invite).count() == 1

    def test_closed_registration_turns_everyone_away(self, tmp_path):
        app = make_app(tmp_path, REGISTRATION="closed")
        first = app.test_client()
        register(first, "owner@example.com")          # bootstrap still allowed

        second = app.test_client()
        resp = register(second, "friend@example.com")
        assert resp.status_code == 403
        assert "Not taking new accounts" in resp.get_data(as_text=True)

    def test_a_spent_invite_stays_spent_after_that_account_is_deleted(self, invite_app):
        """Deleting the invited account clears the reference on the invite —
        that must not turn a used code back into a free way in."""
        owner = invite_app.test_client()
        register(owner, "owner@example.com")
        owner.post("/account/invites", data={})
        with invite_app.app_context():
            code = db.session.query(Invite).one().code

        friend = invite_app.test_client()
        register(friend, "friend@example.com", invite=code)
        friend.post("/account/delete", data={"password": "password123"})

        with invite_app.app_context():
            invite = db.session.query(Invite).one()
            assert invite.used_by_id is None      # the person is gone…
            assert invite.is_used                 # …but the code is still spent

        stranger = invite_app.test_client()
        assert register(stranger, "stranger@example.com", invite=code).status_code == 400

    def test_invite_limit_is_enforced(self, invite_app):
        from app.account import INVITE_LIMIT

        owner = invite_app.test_client()
        register(owner, "owner@example.com")
        for _ in range(INVITE_LIMIT + 3):
            owner.post("/account/invites", data={})

        with invite_app.app_context():
            assert db.session.query(Invite).count() == INVITE_LIMIT


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
        assert "Email or password is wrong." in real.get_data(as_text=True)
        assert "Email or password is wrong." in fake.get_data(as_text=True)

    def test_login_will_not_bounce_you_off_site(self, app):
        client = app.test_client()
        register(client, "m@example.com")
        client.post("/logout")

        resp = client.post("/login?next=https://evil.example.com/steal",
                           data={"email": "m@example.com", "password": "password123"})
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


class TestSharedDevice:
    def test_a_push_endpoint_follows_whoever_signed_in_last(self, tmp_path):
        app = make_app(tmp_path, VAPID_PUBLIC_KEY="pub", VAPID_PRIVATE_KEY="priv",
                       VAPID_CLAIM_EMAIL="mailto:t@example.com")
        payload = {"endpoint": "https://push.example.com/shared",
                   "keys": {"p256dh": "k", "auth": "a"}}

        first = app.test_client()
        register(first, "a@example.com")
        first.post("/api/push/subscribe", json=payload)

        second = app.test_client()
        register(second, "b@example.com")
        second.post("/api/push/subscribe", json=payload)

        with app.app_context():
            rows = db.session.query(PushSubscription).all()
            assert len(rows) == 1
            assert rows[0].user_id == 2       # never both
            db.session.remove()
            db.drop_all()


class TestExport:
    def test_export_contains_the_whole_account(self, app):
        client = app.test_client()
        register(client, "m@example.com")
        upload(client)

        resp = client.get("/account/export")
        assert resp.status_code == 200
        assert "attachment" in resp.headers["Content-Disposition"]

        data = resp.get_json(force=True)
        assert data["profile"]["student_number"] == "60004250098"
        assert len(data["lectures"]) == 126
        assert len(data["subjects"]) == 14
        assert len(data["reports"]) == 1
        assert data["settings"]["overall_limit"] == 75

    def test_export_is_scoped_to_you(self, app):
        first = app.test_client()
        register(first, "a@example.com")
        upload(first)

        second = app.test_client()
        register(second, "b@example.com")
        data = second.get("/account/export").get_json(force=True)

        assert data["lectures"] == []
        assert data["profile"]["email"] == "b@example.com"


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
        for path in ("/account/", "/account/export"):
            assert anon.get(path).status_code == 302
        assert anon.post("/account/delete", data={}).status_code == 302
