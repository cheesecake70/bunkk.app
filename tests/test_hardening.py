"""The production hardening pass: the things that only matter once the app is
on a real domain with strangers pointing browsers at it.

Most of the suite runs with CSRF and rate limiting switched off so that every
other test doesn't have to carry a token or share a counter. The classes here
switch each one back on for exactly the requests that prove it works.
"""
import importlib
import io
import re
from datetime import date, time
from pathlib import Path

import pytest
from reportlab_stub import make_detailed_pdf

import config
from app import create_app, db
from app.mail import OUTBOX
from app.models import ReportSnapshot, Semester, Subject, User

GOLDEN = Path(__file__).parent / "golden" / "detailed_jul_aug.pdf"


def build(tmp_path, config_object="config.TestConfig", **overrides):
    app = create_app(config_object)
    app.config.update(UPLOAD_DIR=str(tmp_path / "uploads"), **overrides)
    with app.app_context():
        db.create_all()
    return app


@pytest.fixture()
def app(tmp_path):
    app = build(tmp_path)
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


def register(client, email="m@example.com", username="muser", password="password123",
             headers=None, **extra):
    return client.post("/register", data={
        "email": email, "username": username,
        "password": password, "confirm_password": password, **extra,
    }, headers=headers or {})


def upload(client, data: bytes, filename="report.pdf"):
    return client.post("/api/reports",
                       data={"report": (io.BytesIO(data), filename)},
                       content_type="multipart/form-data")


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


class CsrfConfig(config.TestConfig):
    WTF_CSRF_ENABLED = True


def token_from(html: str) -> str:
    match = re.search(r'name="csrf-token" content="([^"]+)"', html)
    assert match, "no csrf meta tag on the page"
    return match.group(1)


class TestCsrf:
    @pytest.fixture()
    def app(self, tmp_path):
        app = build(tmp_path, CsrfConfig)
        yield app
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def test_a_form_without_a_token_changes_nothing(self, app):
        client = app.test_client()
        resp = register(client)
        assert resp.status_code == 302            # bounced, with a flash
        with app.app_context():
            assert db.session.query(User).count() == 0

    def test_the_bounce_never_leaves_the_site(self, app):
        """The Referer is attacker-controlled on exactly the request that fails
        this check, so it must not become an open redirect."""
        client = app.test_client()
        off_site = register(client, headers={"Referer": "https://evil.example/x"})
        assert off_site.headers["Location"].endswith("/")
        assert "evil" not in off_site.headers["Location"]
        on_site = register(client, headers={"Referer": "http://localhost/register?x=1"})
        assert on_site.headers["Location"].endswith("/register?x=1")

    def test_a_form_with_the_token_goes_through(self, app):
        client = app.test_client()
        token = token_from(client.get("/register").get_data(as_text=True))
        resp = register(client, csrf_token=token)
        assert resp.status_code == 302 and "/upload" in resp.headers["Location"]
        with app.app_context():
            assert db.session.query(User).count() == 1

    def test_the_json_api_needs_the_header(self, app):
        client = app.test_client()
        token = token_from(client.get("/register").get_data(as_text=True))
        register(client, csrf_token=token)

        bare = client.post("/api/absences", json={"date": "2030-01-01"})
        assert bare.status_code == 400
        assert "expired" in bare.get_json()["error"].lower()

        with_header = client.post("/api/absences", json={"date": "2030-01-01"},
                                  headers={"X-CSRFToken": token})
        assert with_header.status_code != 400

    def test_every_rendered_form_carries_the_token(self, app):
        client = app.test_client()
        token = token_from(client.get("/register").get_data(as_text=True))
        register(client, csrf_token=token)
        for path in ("/settings", "/calendar", "/checkpoints", "/timetable", "/subjects"):
            html = client.get(path).get_data(as_text=True)
            forms = re.findall(r'<form method="post"[^>]*>', html)
            tokens = html.count('name="csrf_token"')
            assert tokens >= len(forms), f"{path}: {len(forms)} forms, {tokens} tokens"


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class LimitedConfig(config.TestConfig):
    RATELIMIT_ENABLED = True
    RATELIMIT_STORAGE_URI = "memory://"


class TestRateLimits:
    @pytest.fixture()
    def app(self, tmp_path):
        app = build(tmp_path, LimitedConfig)
        yield app
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def test_failed_logins_are_throttled_per_client(self, app):
        client = app.test_client()
        codes = [
            client.post("/login", data={"email": f"ghost{i}@x.com", "password": "nope"}).status_code
            for i in range(31)
        ]
        assert codes[:30] == [401] * 30
        assert codes[30] == 429
        page = client.post("/login", data={"email": "x@x.com", "password": "nope"})
        assert page.status_code == 429 and "Too many attempts" in page.get_data(as_text=True)

    def test_successful_logins_do_not_spend_the_allowance(self, app):
        """A campus NAT puts a whole hostel behind one address; sign-ins that
        work must never be what locks the next person out."""
        client = app.test_client()
        register(client)
        client.post("/logout")
        for _ in range(40):
            resp = client.post("/login", data={"email": "m@example.com",
                                               "password": "password123"})
            assert resp.status_code == 302
            client.post("/logout")

    def test_forgot_password_is_throttled(self, app):
        client = app.test_client()
        codes = [client.post("/forgot", data={"email": "who@x.com"}).status_code
                 for i in range(21)]
        assert codes[:20] == [302] * 20 and codes[20] == 429

    def test_registration_is_throttled(self, app):
        client = app.test_client()
        codes = [register(client, email=f"u{i}@x.com", username=f"user{i}").status_code
                 for i in range(61)]
        assert 429 in codes and codes.index(429) == 60

    def test_reads_are_never_throttled(self, app):
        client = app.test_client()
        assert all(client.get("/login").status_code == 200 for _ in range(30))


# ---------------------------------------------------------------------------
# Uploads that are not what they claim
# ---------------------------------------------------------------------------


class TestHostileUploads:
    def test_garbage_named_pdf_is_a_422_not_a_500(self, app):
        client = app.test_client()
        register(client)
        resp = upload(client, b"not a pdf at all" * 100)
        assert resp.status_code == 422
        assert "read that file" in resp.get_json()["error"].lower()

    def test_a_truncated_pdf_is_refused_the_same_way(self, app):
        client = app.test_client()
        register(client)
        resp = upload(client, GOLDEN.read_bytes()[:2000])
        assert resp.status_code == 422

    def test_an_oversize_body_gets_a_json_413(self, app):
        client = app.test_client()
        register(client)
        resp = upload(client, b"%PDF" + b"0" * (6 * 1024 * 1024))
        assert resp.status_code == 413
        assert "too large" in resp.get_json()["error"].lower()

    def test_two_accounts_racing_for_one_student_number(self, app, monkeypatch):
        """Both pass the identity check; the unique index catches the second.
        That has to come back as the same 422 the check would have given."""
        from app import merge

        first = app.test_client()
        register(first, email="a@x.com", username="first")
        assert upload(first, GOLDEN.read_bytes()).status_code == 200

        monkeypatch.setattr(merge, "_check_identity", lambda user, report: None)
        second = app.test_client()
        register(second, email="b@x.com", username="second")
        with app.app_context():
            # Simulate the losing side of the race: the check saw nobody, but by
            # commit time the number belongs to someone.
            user = db.session.query(User).filter_by(email="b@x.com").one()
            user.student_number = None
            db.session.commit()

        def claim_then_ingest(user, report, **kw):
            user.student_number = report.header.student_number
            return original(user, report, **kw)

        original = merge._ingest_report
        monkeypatch.setattr(merge, "_ingest_report", claim_then_ingest)
        resp = upload(second, GOLDEN.read_bytes())
        assert resp.status_code == 422
        assert resp.get_json()["claimed_by"]["username"] == "first"


# ---------------------------------------------------------------------------
# Headers, hosts, errors
# ---------------------------------------------------------------------------


class TestResponseHardening:
    def test_security_headers_on_every_response(self, app):
        resp = app.test_client().get("/login")
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
        assert "Strict-Transport-Security" not in resp.headers   # http in tests

    def test_hsts_rides_with_secure_cookies(self, tmp_path):
        # The after_request hook reads the flag at registration time, so build
        # the app with it set rather than flipping it afterwards.
        class Secure(config.TestConfig):
            SESSION_COOKIE_SECURE = True
        app = build(tmp_path, Secure)
        resp = app.test_client().get("/login")
        assert resp.headers["Strict-Transport-Security"].startswith("max-age=")

    def test_api_answers_json_when_signed_out(self, app):
        resp = app.test_client().post("/api/absences", json={"date": "2030-01-01"})
        assert resp.status_code == 401
        assert resp.is_json

    def test_an_unexpected_error_is_a_json_500_not_a_traceback(self, tmp_path):
        class Live(config.TestConfig):
            TESTING = False
            SECRET_KEY = "not-the-dev-default-" * 3
        app = build(tmp_path, Live)

        @app.get("/api/boom")
        def boom():
            raise RuntimeError("kaboom")

        resp = app.test_client().get("/api/boom")
        assert resp.status_code == 500
        assert "kaboom" not in resp.get_data(as_text=True)
        assert "our side" in resp.get_json()["error"]


class HostConfig(config.TestConfig):
    TRUSTED_HOSTS = ["bunkr.test"]
    SERVER_NAME = None


class TestTrustedHosts:
    @pytest.fixture()
    def app(self, tmp_path):
        app = build(tmp_path, HostConfig)
        yield app
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def test_a_forged_host_cannot_shape_the_reset_link(self, app):
        client = app.test_client()
        register(client, headers={"Host": "bunkr.test"})
        client.post("/logout", headers={"Host": "bunkr.test"})

        forged = client.post("/forgot", data={"email": "m@example.com"},
                             headers={"Host": "evil.example"})
        assert forged.status_code == 400
        assert app.extensions.get(OUTBOX, []) == []

        real = client.post("/forgot", data={"email": "m@example.com"},
                           headers={"Host": "bunkr.test"})
        assert real.status_code == 302
        body = app.extensions[OUTBOX][-1].get_content()
        assert "http://bunkr.test/reset/" in body
        assert "evil" not in body


# ---------------------------------------------------------------------------
# Production boot guard
# ---------------------------------------------------------------------------


class TestProductionGuard:
    REQUIRED = ("SECRET_KEY", "DATABASE_URL", "MAIL_SERVER", "MAIL_DEFAULT_SENDER",
                "BUNKR_TRUSTED_HOSTS")

    def test_missing_variables_are_named(self, monkeypatch):
        for name in self.REQUIRED:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("SECRET_KEY", "x" * 64)
        with pytest.raises(RuntimeError) as err:
            create_app("config.ProdConfig")
        assert "DATABASE_URL" in str(err.value)
        assert "BUNKR_TRUSTED_HOSTS" in str(err.value)

    def test_the_dev_secret_is_refused(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", config.DEV_SECRET_KEY)
        for name in self.REQUIRED[1:]:
            monkeypatch.setenv(name, "set")
        with pytest.raises(RuntimeError) as err:
            create_app("config.ProdConfig")
        assert "development default" in str(err.value)

    def test_a_complete_environment_boots(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SECRET_KEY", "y" * 64)
        monkeypatch.setenv("DATABASE_URL", "sqlite://")
        monkeypatch.setenv("MAIL_SERVER", "smtp.example")
        monkeypatch.setenv("MAIL_DEFAULT_SENDER", "Bunkr <no-reply@example.com>")
        monkeypatch.setenv("BUNKR_TRUSTED_HOSTS", "bunkr.example, www.bunkr.example")
        importlib.reload(config)
        try:
            app = create_app(config.ProdConfig)
        finally:
            importlib.reload(config)
        assert app.config["TRUSTED_HOSTS"] == ["bunkr.example", "www.bunkr.example"]
        assert app.config["REMEMBER_COOKIE_SECURE"] is True
        assert app.config["REMEMBER_COOKIE_SAMESITE"] == "Lax"
        assert app.config["SESSION_COOKIE_SECURE"] is True


# ---------------------------------------------------------------------------
# Semester scoping
# ---------------------------------------------------------------------------


class TestSemesterScoping:
    def test_a_new_terms_report_retires_the_old_term(self, app, tmp_path):
        client = app.test_client()
        register(client)
        assert upload(client, GOLDEN.read_bytes()).status_code == 200
        before = client.get("/api/dashboard").get_json()
        assert len(before["subjects"]) == 14

        fresh = tmp_path / "next.pdf"
        make_detailed_pdf(fresh, [
            ("Compilers", date(2027, 1, 12), time(9, 0, 1), time(10, 0, 1), "P"),
            ("Compilers", date(2027, 1, 19), time(9, 0, 1), time(10, 0, 1), "A"),
        ], period_start=date(2027, 1, 5), period_end=date(2027, 1, 20),
            session="2026-2027, Semester IV")
        assert upload(client, fresh.read_bytes(), "next.pdf").status_code == 200

        after = client.get("/api/dashboard").get_json()
        assert [s["name"] for s in after["subjects"]] == ["Compilers"]
        assert after["overall"]["present"] == 1 and after["overall"]["absent"] == 1
        # Coverage is judged on this term only: July's report no longer counts.
        assert after["coverage"]["gaps"] == []

        with app.app_context():
            active = db.session.query(Semester).filter_by(is_active=True).all()
            assert [s.session_label for s in active] == ["2026-2027, Semester IV"]
            snapshots = db.session.query(ReportSnapshot).order_by(ReportSnapshot.id).all()
            assert [s.semester_id for s in snapshots] == [
                db.session.query(Semester).filter_by(session_label="2026-2027, Semester III").one().id,
                active[0].id,
            ]

        # Pages list only this term's subjects.
        html = client.get("/subjects").get_data(as_text=True)
        assert "Compilers" in html and "Computer Networks" not in html

    def test_old_subjects_stay_reachable_but_out_of_the_totals(self, app, tmp_path):
        client = app.test_client()
        register(client)
        upload(client, GOLDEN.read_bytes())
        with app.app_context():
            old_id = db.session.query(Subject).filter_by(code="CN").one().id
        fresh = tmp_path / "next.pdf"
        make_detailed_pdf(fresh, [
            ("Compilers", date(2027, 1, 12), time(9, 0, 1), time(10, 0, 1), "P"),
        ], period_start=date(2027, 1, 5), period_end=date(2027, 1, 20),
            session="2026-2027, Semester IV")
        upload(client, fresh.read_bytes(), "next.pdf")
        assert client.get(f"/subjects/{old_id}").status_code == 200
