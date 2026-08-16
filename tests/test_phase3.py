"""Phase 3: PWA shell, push plumbing, and the morning brief.

Push delivery itself is stubbed — the value here is in the wiring around it:
that a brief says something true and useful in every mode, that dead
subscriptions get pruned instead of retried forever, and that the cron only
wakes the users who asked to be woken.
"""
from datetime import date, timedelta
from pathlib import Path

import pytest

from app import create_app, db
from app.models import PushSubscription, Settings, User

GOLDEN = Path(__file__).parent / "golden" / "detailed_jul_aug.pdf"
TODAY = date(2026, 8, 16)
SEMESTER_END = date(2026, 11, 28)


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


@pytest.fixture()
def push_app(app):
    app.config.update(
        VAPID_PUBLIC_KEY="test-public-key",
        VAPID_PRIVATE_KEY="test-private-key",
        VAPID_CLAIM_EMAIL="mailto:test@example.com",
    )
    return app


@pytest.fixture()
def frozen(monkeypatch):
    import app.planning as planning_module

    class FrozenDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(planning_module, "date", FrozenDate)


@pytest.fixture()
def client(app):
    client = app.test_client()
    client.post("/register", data={"email": "m@example.com", "password": "password123"})
    return client


def upload(client):
    return client.post(
        "/api/reports",
        data={"report": (GOLDEN.open("rb"), "report.pdf")},
        content_type="multipart/form-data",
    )


def setup_advanced(client):
    import re

    upload(client)
    page = client.get("/timetable").get_data(as_text=True)
    tokens = re.findall(r'name="slot"\s+value="([^"]+)"\s+checked', page)
    client.post("/timetable", data={"slot": tokens})
    client.post("/calendar/end-date", data={"end_date": SEMESTER_END.isoformat()})


def subscribe(client, endpoint="https://push.example.com/abc"):
    return client.post("/api/push/subscribe", json={
        "endpoint": endpoint,
        "keys": {"p256dh": "test-p256dh", "auth": "test-auth"},
        "user_agent": "pytest",
    })


class TestPwaShell:
    def test_manifest_is_served_and_installable(self, client):
        resp = client.get("/static/manifest.webmanifest")
        assert resp.status_code == 200
        manifest = resp.get_json(force=True)

        assert manifest["start_url"] == "/"
        assert manifest["display"] == "standalone"
        sizes = {icon["sizes"] for icon in manifest["icons"]}
        assert {"192x192", "512x512"} <= sizes
        assert any(icon.get("purpose") == "maskable" for icon in manifest["icons"])

    def test_icons_exist(self, client):
        for name in ("icon-192.png", "icon-512.png", "apple-touch-icon.png"):
            resp = client.get(f"/static/icons/{name}")
            assert resp.status_code == 200
            assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_service_worker_is_served_at_root_scope(self, client):
        """From /static/ it could only ever control /static/."""
        resp = client.get("/sw.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers["Content-Type"]
        assert resp.headers["Service-Worker-Allowed"] == "/"

    def test_service_worker_never_caches_pages(self, client):
        """Cached attendance data on a shared device is a privacy failure."""
        source = client.get("/sw.js").get_data(as_text=True)
        assert 'url.pathname.startsWith("/api/")' in source
        navigate = source.split('request.mode === "navigate"')[1].split("}")[0]
        assert "cache.put" not in navigate

    def test_offline_page_works_without_a_session(self, client):
        client.post("/logout")
        resp = client.get("/offline")
        assert resp.status_code == 200
        assert "No connection" in resp.get_data(as_text=True)

    def test_pages_advertise_the_manifest(self, client):
        html = client.get("/").get_data(as_text=True)
        assert "manifest.webmanifest" in html
        assert "apple-touch-icon" in html


class TestMorningBrief:
    def test_basic_mode_brief_points_at_the_pending_pile(self, client, app, frozen):
        upload(client)
        with app.app_context():
            from app import planning
            from app.models import User as U

            brief = planning.morning_brief(db.session.get(U, 1))
        assert brief is not None
        assert "unmarked" in brief.title
        assert brief.url == "/upload"

    def test_advanced_brief_names_tomorrows_verdict(self, client, app, frozen):
        setup_advanced(client)
        with app.app_context():
            from app import planning
            from app.models import User as U

            brief = planning.morning_brief(db.session.get(U, 1))
        # 17 Aug 2026 is a Monday with 7 lectures.
        assert brief.title == "Tomorrow: skippable"
        assert "DBMS Lab" in brief.body

    def test_brief_carries_the_unlock_nudge(self, client, app, frozen):
        setup_advanced(client)
        with app.app_context():
            from app import planning
            from app.models import User as U

            user = db.session.get(U, 1)
            estimate = planning.unlock_estimate(user)
            brief = planning.morning_brief(user)

        assert estimate.worth_nudging
        assert estimate.optimistic_budget > estimate.current_budget
        assert f"unlock up to {estimate.gain} more" in brief.body

    def test_a_quiet_ledger_says_nothing(self, app):
        """No data, no news — the cron shouldn't invent a notification."""
        with app.app_context():
            from app import planning

            user = User(email="quiet@example.com")
            user.set_password("password123")
            db.session.add(user)
            db.session.flush()
            db.session.add(Settings(user_id=user.id))
            db.session.commit()
            assert planning.morning_brief(user) is None


class TestPushEndpoints:
    def test_key_endpoint_reports_configuration(self, client, app):
        assert client.get("/api/push/key").get_json()["configured"] is False

    def test_subscribing_stores_the_device_and_opts_in(self, push_app):
        client = push_app.test_client()
        client.post("/register", data={"email": "m@example.com", "password": "password123"})

        assert subscribe(client).status_code == 200
        with push_app.app_context():
            sub = db.session.query(PushSubscription).one()
            assert sub.user_id == 1
            assert db.session.get(Settings, 1).notify_enabled is True

    def test_subscribing_twice_is_idempotent(self, push_app):
        client = push_app.test_client()
        client.post("/register", data={"email": "m@example.com", "password": "password123"})
        subscribe(client)
        subscribe(client)
        with push_app.app_context():
            assert db.session.query(PushSubscription).count() == 1

    def test_an_endpoint_moving_to_another_account_is_reassigned(self, push_app):
        """Browsers reuse endpoints; two accounts must never share one device row."""
        first = push_app.test_client()
        first.post("/register", data={"email": "a@example.com", "password": "password123"})
        subscribe(first)

        second = push_app.test_client()
        second.post("/register", data={"email": "b@example.com", "password": "password123"})
        subscribe(second)

        with push_app.app_context():
            rows = db.session.query(PushSubscription).all()
            assert len(rows) == 1
            assert rows[0].user_id == 2

    def test_unsubscribing_removes_the_device_and_opts_out(self, push_app):
        client = push_app.test_client()
        client.post("/register", data={"email": "m@example.com", "password": "password123"})
        subscribe(client)

        client.post("/api/push/unsubscribe", json={"endpoint": "https://push.example.com/abc"})
        with push_app.app_context():
            assert db.session.query(PushSubscription).count() == 0
            assert db.session.get(Settings, 1).notify_enabled is False

    def test_incomplete_subscription_is_refused(self, push_app):
        client = push_app.test_client()
        client.post("/register", data={"email": "m@example.com", "password": "password123"})
        resp = client.post("/api/push/subscribe", json={"endpoint": "https://x/y"})
        assert resp.status_code == 400

    def test_test_push_needs_configuration(self, client):
        assert client.post("/api/push/test").status_code == 400

    def test_endpoints_need_a_session(self, app):
        anon = app.test_client()
        assert anon.post("/api/push/subscribe", json={}).status_code == 302


class TestDelivery:
    def _stub(self, monkeypatch, outcome):
        """Replace pywebpush.webpush with a recorder / failure injector."""
        calls = []

        class FakeResponse:
            def __init__(self, status_code):
                self.status_code = status_code

        import pywebpush

        class FakeException(Exception):
            def __init__(self, status):
                super().__init__("boom")
                self.response = FakeResponse(status)

        def fake_webpush(**kwargs):
            calls.append(kwargs)
            if outcome is not None:
                raise pywebpush.WebPushException("failed", response=FakeResponse(outcome))

        monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
        return calls

    def test_a_gone_subscription_is_deleted_not_retried(self, push_app, monkeypatch):
        client = push_app.test_client()
        client.post("/register", data={"email": "m@example.com", "password": "password123"})
        subscribe(client)
        self._stub(monkeypatch, outcome=410)

        with push_app.app_context():
            from app import push as push_module

            sub = db.session.query(PushSubscription).one()
            assert push_module.send(sub, {"title": "hi"}) is False
            assert db.session.query(PushSubscription).count() == 0

    def test_a_transient_failure_keeps_the_subscription(self, push_app, monkeypatch):
        client = push_app.test_client()
        client.post("/register", data={"email": "m@example.com", "password": "password123"})
        subscribe(client)
        self._stub(monkeypatch, outcome=503)

        with push_app.app_context():
            from app import push as push_module

            sub = db.session.query(PushSubscription).one()
            assert push_module.send(sub, {"title": "hi"}) is False
            assert db.session.query(PushSubscription).count() == 1

    def test_successful_send_records_the_timestamp(self, push_app, monkeypatch):
        client = push_app.test_client()
        client.post("/register", data={"email": "m@example.com", "password": "password123"})
        subscribe(client)
        calls = self._stub(monkeypatch, outcome=None)

        with push_app.app_context():
            from app import push as push_module

            sub = db.session.query(PushSubscription).one()
            assert push_module.send(sub, {"title": "hi"}) is True
            assert db.session.query(PushSubscription).one().last_sent_at is not None
        assert len(calls) == 1


class TestCronTargeting:
    def _user(self, email, hour, enabled=True):
        user = User(email=email)
        user.set_password("password123")
        db.session.add(user)
        db.session.flush()
        db.session.add(Settings(user_id=user.id, notify_enabled=enabled, notify_hour=hour))
        db.session.add(PushSubscription(
            user_id=user.id, endpoint=f"https://push.example.com/{email}",
            p256dh="k", auth="a",
        ))
        db.session.commit()
        return user

    def test_only_users_scheduled_for_this_hour_are_woken(self, push_app, monkeypatch):
        import pywebpush

        sent = []
        monkeypatch.setattr(pywebpush, "webpush", lambda **kw: sent.append(kw))

        with push_app.app_context():
            from app import planning, push as push_module

            self._user("seven@example.com", hour=7)
            self._user("nine@example.com", hour=9)
            self._user("optedout@example.com", hour=7, enabled=False)

            monkeypatch.setattr(
                planning, "morning_brief",
                lambda user, today=None: planning.Brief(title="t", body="b"),
            )
            summary = push_module.send_morning_briefs(hour=7)

        assert summary["considered"] == 1
        assert summary["sent"] == 1

    def test_users_with_no_news_are_skipped(self, push_app, monkeypatch):
        with push_app.app_context():
            from app import planning, push as push_module

            self._user("seven@example.com", hour=7)
            monkeypatch.setattr(planning, "morning_brief", lambda user, today=None: None)
            summary = push_module.send_morning_briefs(hour=7)

        assert summary["considered"] == 1
        assert summary["sent"] == 0
        assert summary["skipped_no_news"] == 1


class TestVapidKeygen:
    def test_generated_keys_are_the_right_shape_and_load(self, app):
        """VAPID keys are EC P-256: a 65-byte uncompressed point and a 32-byte
        scalar. The Ed25519-style `*_bytes_raw()` helpers don't exist for them."""
        import base64

        from py_vapid import Vapid01

        with app.app_context():
            from app.push import generate_vapid_keys

            keys = generate_vapid_keys()

        def decode(value):
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

        assert len(decode(keys["public"])) == 65
        assert decode(keys["public"])[0] == 0x04        # uncompressed point marker
        assert len(decode(keys["private"])) == 32
        assert Vapid01.from_raw(keys["private"].encode()).private_key is not None


class TestSettingsUi:
    def test_notification_card_explains_itself_when_unconfigured(self, client):
        html = client.get("/settings").get_data(as_text=True)
        assert "Morning notification" in html
        assert 'data-configured="0"' in html

    def test_notify_hour_round_trips(self, client, app):
        client.post("/settings", data={"only_notify_hour": "1", "notify_hour": "6"},
                    follow_redirects=True)
        with app.app_context():
            assert db.session.get(Settings, 1).notify_hour == 6

    def test_notify_hour_is_validated(self, client, app):
        resp = client.post("/settings", data={"only_notify_hour": "1", "notify_hour": "25"})
        assert resp.status_code == 400
        with app.app_context():
            assert db.session.get(Settings, 1).notify_hour == 7

    def test_saving_limits_does_not_disturb_the_hour(self, client, app):
        client.post("/settings", data={"only_notify_hour": "1", "notify_hour": "6"})
        client.post("/settings", data={
            "subject_limit": "80", "overall_limit": "85", "staleness_days": "3"})
        with app.app_context():
            settings = db.session.get(Settings, 1)
            assert (settings.notify_hour, settings.subject_limit) == (6, 80)
