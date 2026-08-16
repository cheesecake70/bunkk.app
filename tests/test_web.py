"""Web-layer tests: auth, the upload API, and every rendered page.

Includes the isolation checks that make the multi-tenant schema real rather
than aspirational (ADR-5) — one user must never see another's ledger.
"""
from datetime import date, time
from pathlib import Path

import pytest
from reportlab_stub import make_detailed_pdf

from app import create_app, db
from app.models import Subject, User

GOLDEN = Path(__file__).parent / "golden" / "detailed_jul_aug.pdf"
SUMMARY = Path(__file__).parent / "golden" / "summary_july.pdf"


@pytest.fixture()
def app(tmp_path):
    """An app with no app context held open.

    This matters: Flask-Login caches the signed-in user on `g`, and Flask reuses
    an already-pushed app context instead of creating one per request. A fixture
    that wraps the whole test in `app.app_context()` therefore leaks one client's
    identity into the next — which would quietly defeat the isolation tests
    below. Tests that need direct DB access push their own context.
    """
    app = create_app("config.TestConfig")
    app.config.update(UPLOAD_DIR=str(tmp_path / "uploads"), WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def register(client, email="m@example.com", password="password123"):
    return client.post("/register", data={"email": email, "password": password},
                       follow_redirects=False)


def upload(client, path=GOLDEN, filename="report.pdf"):
    return client.post(
        "/api/reports",
        data={"report": (path.open("rb"), filename)},
        content_type="multipart/form-data",
    )


class TestAuth:
    def test_register_signs_you_in_and_sends_you_to_upload(self, client):
        resp = register(client)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/upload")

    def test_register_rejects_a_short_password(self, client):
        resp = client.post("/register", data={"email": "a@b.com", "password": "short"})
        assert resp.status_code == 400
        assert b"at least 8 characters" in resp.data.lower()

    def test_register_rejects_a_duplicate_email(self, client):
        register(client)
        client.post("/logout")
        resp = client.post("/register", data={"email": "m@example.com", "password": "password123"})
        assert resp.status_code == 400
        assert b"already has an account" in resp.data

    def test_login_with_a_wrong_password_fails(self, client):
        register(client)
        client.post("/logout")
        resp = client.post("/login", data={"email": "m@example.com", "password": "nope12345"})
        assert resp.status_code == 401

    def test_login_then_dashboard(self, client):
        register(client)
        client.post("/logout")
        resp = client.post("/login", data={"email": "m@example.com", "password": "password123"})
        assert resp.status_code == 302
        assert client.get("/").status_code == 200

    @pytest.mark.parametrize("path", ["/", "/upload", "/history", "/settings"])
    def test_pages_require_a_session(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_upload_api_requires_a_session(self, client):
        assert upload(client).status_code == 302


class TestUploadApi:
    def test_happy_path_returns_the_diff(self, client):
        register(client)
        resp = upload(client)

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["status"] == "merged"
        assert body["added"] == 126
        assert len(body["new_subjects"]) == 14

    def test_reupload_is_reported_as_a_duplicate(self, client):
        register(client)
        upload(client)
        body = upload(client).get_json()
        assert body["status"] == "duplicate"

    def test_summary_pdf_is_politely_redirected(self, client):
        register(client)
        resp = upload(client, SUMMARY, "summary.pdf")
        assert resp.status_code == 422
        assert "detailed" in resp.get_json()["error"].lower()

    def test_non_pdf_is_refused(self, client, tmp_path):
        register(client)
        junk = tmp_path / "notes.txt"
        junk.write_text("hello")
        resp = client.post("/api/reports",
                           data={"report": (junk.open("rb"), "notes.txt")},
                           content_type="multipart/form-data")
        assert resp.status_code == 400

    def test_missing_file_is_refused(self, client):
        register(client)
        resp = client.post("/api/reports", data={}, content_type="multipart/form-data")
        assert resp.status_code == 400

    def test_dashboard_api_shape(self, client):
        register(client)
        upload(client)
        body = client.get("/api/dashboard").get_json()

        assert len(body["subjects"]) == 14
        assert body["overall"]["limit"] == 75
        assert 0 <= body["overall"]["worst_pct"] <= 100
        ds = next(s for s in body["subjects"] if s["code"] == "DS")
        assert (ds["present"], ds["absent"], ds["pending"]) == (7, 2, 3)


class TestUploadDiffOverTheApi:
    """The golden report only ever *adds* lectures, so the JSON shape of a
    status change went untested until a real upload hit it. These two synthetic
    exports cover the resolving path end to end."""

    def _week(self, path, statuses):
        rows = [
            ("Computer NetworksT C2", date(2026, 8, 13), time(10, 0, 1), time(11, 0, 0), statuses[0]),
            ("Data StructuresT C2", date(2026, 8, 13), time(13, 0, 1), time(14, 0, 0), statuses[1]),
        ]
        make_detailed_pdf(path, rows,
                          period_start=date(2026, 8, 13), period_end=date(2026, 8, 20))
        return path

    def test_status_changes_serialise(self, client, tmp_path):
        register(client)
        first = self._week(tmp_path / "w1.pdf", ["NU", "NU"])
        second = self._week(tmp_path / "w2.pdf", ["P", "A"])

        assert upload(client, first, "w1.pdf").status_code == 200
        resp = upload(client, second, "w2.pdf")

        assert resp.status_code == 200, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["updated"] == 2
        assert body["resolved_pending"] == 2

        change = next(c for c in body["changes"] if c["subject_code"] == "CN")
        assert change["on_date"] == "2026-08-13"
        assert change["start_time"] == "10:00:01"     # a `time`, not a raw object
        assert (change["from_status"], change["to_status"]) == ("NU", "P")

    def test_vanished_rows_serialise(self, client, tmp_path):
        register(client)
        upload(client, self._week(tmp_path / "w1.pdf", ["NU", "NU"]), "w1.pdf")

        shrunk = tmp_path / "w2.pdf"
        make_detailed_pdf(
            shrunk,
            [("Computer NetworksT C2", date(2026, 8, 13), time(10, 0, 1), time(11, 0, 0), "P")],
            period_start=date(2026, 8, 13), period_end=date(2026, 8, 20),
        )
        body = upload(client, shrunk, "w2.pdf").get_json()

        assert len(body["vanished"]) == 1
        assert body["vanished"][0]["subject_code"] == "DS"
        assert body["vanished"][0]["start_time"] == "13:00:01"

    def test_alias_question_serialises(self, client, tmp_path):
        register(client)
        upload(client, self._week(tmp_path / "w1.pdf", ["P", "P"]), "w1.pdf")

        renamed = tmp_path / "w2.pdf"
        make_detailed_pdf(
            renamed,
            [("Computer NetworkT C2", date(2026, 8, 14), time(10, 0, 1), time(11, 0, 0), "P")],
            period_start=date(2026, 8, 13), period_end=date(2026, 8, 20),
        )
        body = upload(client, renamed, "w2.pdf").get_json()

        assert body["status"] == "needs_confirmation"
        assert body["proposals"][0]["match_code"] == "CN"

        resolved = client.post(
            f"/api/reports/{body['snapshot_id']}/resolve",
            json={"decisions": {
                body["proposals"][0]["raw_name"]:
                    f"merge:{body['proposals'][0]['match_subject_id']}"
            }},
        )
        assert resolved.status_code == 200
        assert resolved.get_json()["status"] == "merged"


class TestPages:
    @pytest.fixture()
    def loaded(self, client):
        register(client)
        upload(client)
        return client

    def test_dashboard_shows_the_budget_and_subjects(self, loaded):
        html = loaded.get("/").get_data(as_text=True)
        assert "Bunks left overall" in html
        assert "DBMS Lab" in html
        assert "Worst-case %" in html

    def test_dashboard_flags_the_pending_pile(self, loaded):
        html = loaded.get("/").get_data(as_text=True)
        assert "pending lectures" in html

    def test_empty_dashboard_asks_for_a_pdf(self, client):
        register(client)
        html = client.get("/").get_data(as_text=True)
        assert "No report yet" in html
        assert "Upload your PDF" in html

    def test_subject_page_lists_every_lecture(self, loaded, app):
        with app.app_context():
            subject_id = db.session.query(Subject).filter_by(code="DS").one().id
        html = loaded.get(f"/subjects/{subject_id}").get_data(as_text=True)

        assert "Every lecture" in html
        assert "Worst case" in html
        assert html.count('class="lecture ') == 12   # 7 P + 2 A + 3 NU

    def test_history_lists_the_upload(self, loaded):
        html = loaded.get("/history").get_data(as_text=True)
        assert "01.07 → 12.08" in html
        assert "126" in html

    def test_settings_round_trip(self, loaded):
        resp = loaded.post("/settings", data={
            "subject_limit": "80", "overall_limit": "85", "staleness_days": "3",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert "80" in resp.get_data(as_text=True)

        body = loaded.get("/api/dashboard").get_json()
        assert body["overall"]["limit"] == 85

    def test_settings_rejects_an_impossible_percentage(self, loaded):
        resp = loaded.post("/settings", data={
            "subject_limit": "110", "overall_limit": "75", "staleness_days": "7",
        })
        assert resp.status_code == 400
        assert "between 0 and 100" in resp.get_data(as_text=True)

    def test_per_subject_override_changes_that_subjects_limit(self, loaded, app):
        with app.app_context():
            subject_id = db.session.query(Subject).filter_by(code="DS").one().id
        loaded.post("/settings", data={
            "subject_limit": "70", "overall_limit": "75", "staleness_days": "7",
            f"custom_limit_{subject_id}": "50",
        })
        body = loaded.get("/api/dashboard").get_json()
        ds = next(s for s in body["subjects"] if s["code"] == "DS")
        assert ds["limit"] == 50
        assert ds["verdict"] != "danger"     # 58.3% worst case now clears 50%


class TestUserIsolation:
    def test_one_user_never_sees_anothers_data(self, app, client):
        register(client, "first@example.com")
        upload(client)
        with app.app_context():
            subject_id = db.session.query(Subject).first().id
        client.post("/logout")

        other = app.test_client()
        register(other, "second@example.com")

        assert other.get("/api/dashboard").get_json()["subjects"] == []
        assert other.get(f"/subjects/{subject_id}").status_code == 404

    def test_a_second_user_cannot_resolve_someone_elses_upload(self, app, client):
        """Alias answers are scoped to the uploader, not just to the snapshot id."""
        register(client, "first@example.com")
        upload(client)

        other = app.test_client()
        register(other, "second@example.com")
        resp = other.post("/api/reports/1/resolve", json={"decisions": {}})
        assert resp.status_code == 422

    def test_settings_are_per_user(self, app, client):
        register(client, "first@example.com")
        client.post("/settings", data={
            "subject_limit": "90", "overall_limit": "95", "staleness_days": "2"})

        other = app.test_client()
        register(other, "second@example.com")
        assert other.get("/api/dashboard").get_json()["overall"]["limit"] == 75


def test_users_table_stays_clean(app, client):
    register(client)
    with app.app_context():
        assert db.session.query(User).count() == 1
