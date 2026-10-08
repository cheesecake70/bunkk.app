"""Usage tracking: sign-ins, last-seen, events, and who may read the totals."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from conftest import google_sign_in

from app import create_app, db, tracking
from app.models import Event, User

GOLDEN = Path(__file__).parent / "golden" / "detailed_jul_aug.pdf"


@pytest.fixture()
def app(tmp_path):
    app = create_app("config.TestConfig")
    app.config.update(UPLOAD_DIR=str(tmp_path / "uploads"),
                      ADMIN_EMAILS=frozenset({"boss@example.com"}))
    with app.app_context():
        db.create_all()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def upload(client):
    return client.post(
        "/api/reports",
        data={"report": (GOLDEN.open("rb"), "report.pdf")},
        content_type="multipart/form-data",
    )


def names(app, email="m@example.com"):
    with app.app_context():
        user = db.session.query(User).filter_by(email=email).one()
        rows = db.session.query(Event).filter_by(user_id=user.id).order_by(Event.id)
        return [e.name for e in rows]


def backdate_seen(app, **delta):
    with app.app_context():
        user = db.session.query(User).one()
        user.last_seen_at = datetime.now(timezone.utc) - timedelta(**delta)
        db.session.commit()


class TestSignIns:
    def test_the_first_sign_in_is_a_signup_a_login_and_a_visit(self, app):
        google_sign_in(app.test_client())
        assert sorted(names(app)) == ["login", "signup", "visit"]
        with app.app_context():
            user = db.session.query(User).one()
            assert user.last_login_at is not None
            assert user.last_seen_at is not None

    def test_signing_in_again_is_a_login_but_not_a_signup(self, app):
        google_sign_in(app.test_client())
        google_sign_in(app.test_client())
        assert sorted(names(app)) == ["login", "login", "signup", "visit"]

    def test_a_failed_sign_in_records_nothing(self, app):
        google_sign_in(app.test_client(), verified=False)
        with app.app_context():
            assert db.session.query(Event).count() == 0


class TestLastSeen:
    def test_browsing_within_the_hour_writes_nothing(self, app):
        client = app.test_client()
        google_sign_in(client)
        with app.app_context():
            before = db.session.query(User).one().last_seen_at
        client.get("/settings")
        client.get("/upload")
        with app.app_context():
            assert db.session.query(User).one().last_seen_at == before
        assert names(app).count("visit") == 1

    def test_coming_back_later_the_same_day_refreshes_the_stamp_only(self, app):
        client = app.test_client()
        google_sign_in(client)
        now = datetime.now(timezone.utc)
        # Same UTC day, more than an hour ago — unless it is just past midnight.
        with app.app_context():
            user = db.session.query(User).one()
            user.last_seen_at = max(now - timedelta(hours=2),
                                    now.replace(hour=0, minute=0, second=0, microsecond=0))
            stale = user.last_seen_at
            db.session.commit()
        if now - stale < tracking.SEEN_EVERY:
            pytest.skip("too close to midnight UTC to stay on the same day")
        client.get("/settings")
        with app.app_context():
            seen = db.session.query(User).one().last_seen_at
            assert seen > stale.replace(tzinfo=None)
        assert names(app).count("visit") == 1

    def test_the_first_request_of_a_new_day_is_a_visit(self, app):
        client = app.test_client()
        google_sign_in(client)
        backdate_seen(app, days=2)
        client.get("/settings")
        assert names(app).count("visit") == 2

    def test_static_files_do_not_count(self, app):
        client = app.test_client()
        google_sign_in(client)
        backdate_seen(app, days=2)
        client.get("/static/css/app.css")
        client.get("/healthz")
        assert names(app).count("visit") == 1


class TestActions:
    def test_an_upload_is_recorded_once(self, app):
        client = app.test_client()
        google_sign_in(client)
        assert upload(client).status_code == 200
        upload(client)          # the identical file is a duplicate, not an upload
        assert names(app).count("report_uploaded") == 1

    def test_a_rejected_action_leaves_no_event(self, app):
        client = app.test_client()
        google_sign_in(client)
        client.post("/checkpoints", data={"on_date": "2000-01-01"})
        assert "checkpoint_added" not in names(app)

    def test_deleting_the_account_deletes_its_events(self, app):
        keeper = app.test_client()
        google_sign_in(keeper, "keeper@example.com")
        leaver = app.test_client()
        google_sign_in(leaver, "leaver@example.com")
        upload(leaver)
        leaver.post("/account/delete", data={"confirm": "leaver"})
        with app.app_context():
            keeper_id = db.session.query(User).one().id
            assert {e.user_id for e in db.session.query(Event)} == {keeper_id}


class TestStats:
    def test_the_page_is_invisible_to_everyone_else(self, app):
        assert app.test_client().get("/admin/stats").status_code == 302
        client = app.test_client()
        google_sign_in(client, "student@example.com")
        assert client.get("/admin/stats").status_code == 404

    def test_an_admin_sees_the_totals(self, app):
        student = app.test_client()
        google_sign_in(student, "student@example.com")
        upload(student)
        admin = app.test_client()
        google_sign_in(admin, "Boss@Example.com")
        resp = admin.get("/admin/stats")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert "report_uploaded" in body and "student@example.com" in body

    def test_the_summary_counts_people_not_requests(self, app):
        for email in ("a@example.com", "b@example.com"):
            client = app.test_client()
            google_sign_in(client, email)
            client.get("/settings")
        upload(client)
        with app.app_context():
            data = tracking.summary()
        assert data["users"]["total"] == 2
        assert data["users"]["with_report"] == 1
        assert data["users"]["active"] == {"1d": 2, "7d": 2, "30d": 2}
        assert data["users"]["signups"]["7d"] == 2
        today = data["daily"][0]
        assert (today["active"], today["signups"], today["uploads"]) == (2, 2, 1)
        by_name = {e["name"]: e for e in data["events"]}
        assert by_name["login"]["users_30d"] == 2

    def test_the_cli_prints_the_same_figures(self, app):
        google_sign_in(app.test_client())
        result = app.test_cli_runner().invoke(args=["stats"])
        assert result.exit_code == 0, result.output
        assert "Accounts          1" in result.output
        assert "signup" in result.output
