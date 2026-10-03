"""Web-layer tests: auth, the upload API, and every rendered page.

Includes the isolation checks that make the multi-tenant schema real rather
than aspirational (ADR-5) — one user must never see another's ledger.
"""
from datetime import date, time
from pathlib import Path

import pytest
from conftest import google_sign_in
from reportlab_stub import make_detailed_pdf

from app import create_app, db
from app.models import LectureInstance, Subject, User

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
    app.config.update(UPLOAD_DIR=str(tmp_path / "uploads"))
    with app.app_context():
        db.create_all()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def register(client, email="m@example.com", username=None):
    return google_sign_in(client, email, username)


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

    def test_register_is_just_the_login_page(self, client):
        resp = client.get("/register")
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")

    def test_login_page_offers_google_only(self, client):
        html = client.get("/login").get_data(as_text=True)
        assert "/login/google" in html
        assert 'type="password"' not in html

    def test_an_unverified_google_address_is_refused(self, client):
        resp = google_sign_in(client, "m@example.com", verified=False)
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")
        assert client.get("/").status_code == 302

    def test_signing_in_again_lands_on_the_dashboard(self, client):
        register(client)
        client.post("/logout")
        resp = register(client)
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/")
        assert client.get("/").status_code == 200

    @pytest.mark.parametrize("path", ["/", "/upload", "/settings"])
    def test_pages_require_a_session(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_upload_api_requires_a_session(self, client):
        # JSON, not a redirect: fetch() can act on a 401, never on a login page.
        resp = upload(client)
        assert resp.status_code == 401
        assert "sign in" in resp.get_json()["error"].lower()


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

    def test_plan_shows_the_subjects_and_the_totals(self, loaded):
        html = loaded.get("/plan").get_data(as_text=True)
        assert "DBMS Lab" in html
        assert "Attended" in html

    def test_plan_shows_best_case_beside_the_current_figure(self, loaded):
        """Worst case decides; best case says how much of the gap is unknowns."""
        html = loaded.get("/plan").get_data(as_text=True)
        assert "Best case" in html

    def test_plan_flags_the_pending_pile(self, loaded):
        html = loaded.get("/plan").get_data(as_text=True)
        assert "pending lectures" in html

    def test_plan_drops_the_lecture_type_column(self, loaded):
        """One row per subject, no column restating what the code already says."""
        html = loaded.get("/plan").get_data(as_text=True)
        assert "<th class=\"col-optional\">Type</th>" not in html

    def test_overview_redirects_into_plan(self, loaded):
        """The two pages answered the same question; the old address still works."""
        resp = loaded.get("/overview")
        assert resp.status_code == 301
        assert resp.headers["Location"].endswith("/plan")

    def test_today_does_not_carry_the_semester_table(self, loaded):
        """The split is the point: Today answers one question and stops."""
        html = loaded.get("/").get_data(as_text=True)
        assert "Bunks left" not in html
        assert "Planned absences" not in html
        assert "Best case" not in html

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

    def test_upload_page_shows_what_is_covered(self, loaded):
        """Coverage lives on Upload — the page you'd act on it from."""
        html = loaded.get("/upload").get_data(as_text=True)
        assert "01.07 → 12.08" in html

    def test_settings_round_trip(self, loaded):
        resp = loaded.post("/settings", data={
            "subject_limit": "80", "overall_limit": "85",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert "80" in resp.get_data(as_text=True)

        body = loaded.get("/api/dashboard").get_json()
        assert body["overall"]["limit"] == 85

    def test_settings_rejects_an_impossible_percentage(self, loaded):
        resp = loaded.post("/settings", data={
            "subject_limit": "110", "overall_limit": "75",
        })
        assert resp.status_code == 400
        assert "between 0 and 100" in resp.get_data(as_text=True)

    def test_per_subject_override_changes_that_subjects_limit(self, loaded, app):
        with app.app_context():
            subject_id = db.session.query(Subject).filter_by(code="DS").one().id
        loaded.post("/subjects", data={
            f"name_{subject_id}": "Data Structures",
            f"code_{subject_id}": "DS",
            f"custom_limit_{subject_id}": "50",
        })
        body = loaded.get("/api/dashboard").get_json()
        ds = next(s for s in body["subjects"] if s["code"] == "DS")
        assert ds["limit"] == 50
        assert ds["verdict"] != "danger"     # 58.3% worst case now clears 50%


    def test_the_phone_tab_bar_and_setup_sheet_are_there(self, loaded):
        """The top nav wrapped into three rows on a phone; the four places
        anyone goes now live in a bottom bar, with Setup as a sheet."""
        html = loaded.get("/").get_data(as_text=True)
        assert 'class="tabbar"' in html
        assert 'id="setup-sheet"' in html

    def test_signed_out_pages_have_no_tab_bar(self, client):
        html = client.get("/login").get_data(as_text=True)
        assert "tabbar" not in html


class TestSettingsPageShape:
    @pytest.fixture()
    def loaded(self, client):
        register(client)
        upload(client)
        return client

    def test_the_staleness_knob_is_gone_but_the_warning_is_not(self, loaded, app):
        """Nobody tunes "warn me after N days". The default still drives the
        banner that says how old your newest report is."""
        html = loaded.get("/settings").get_data(as_text=True)
        assert "Warn me after" not in html
        assert "staleness_days" not in html

        with app.app_context():
            from app.models import Settings

            assert db.session.query(Settings).one().staleness_days == 7

    def test_setup_links_to_the_four_pages_you_set_up_once(self, loaded):
        html = loaded.get("/settings").get_data(as_text=True)
        for label in ("Edit timetable", "Calendar", "Checkpoints", "Subjects"):
            assert label in html, f"{label} tile missing from Settings"
        for path in ("/timetable", "/calendar", "/checkpoints", "/subjects"):
            assert f'href="{path}"' in html

    def test_per_subject_limits_no_longer_have_their_own_box(self, loaded):
        html = loaded.get("/settings").get_data(as_text=True)
        assert "Per-subject limits" not in html


class TestSubjectsPage:
    @pytest.fixture()
    def loaded(self, client):
        register(client)
        upload(client)
        return client

    def test_one_subject_saves_on_its_own(self, loaded, app):
        """Renaming one course shouldn't depend on finding a button three
        screens down."""
        with app.app_context():
            subject_id = db.session.query(Subject).filter_by(code="DS").one().id

        resp = loaded.put(f"/api/subjects/{subject_id}", json={"code": "DSA"})
        assert resp.status_code == 200
        assert resp.get_json()["subject"]["code"] == "DSA"

        with app.app_context():
            subject = db.session.get(Subject, subject_id)
            assert subject.code == "DSA"
            # The fields the request left out are untouched, not cleared.
            assert subject.canonical_name

    def test_a_clashing_code_is_refused_the_same_way_either_route(self, loaded, app):
        with app.app_context():
            rows = db.session.query(Subject).order_by(Subject.code).all()
            first, second = rows[0].id, rows[1].id
            taken = rows[0].code

        resp = loaded.put(f"/api/subjects/{second}", json={"code": taken})
        assert resp.status_code == 422
        assert "already uses" in resp.get_json()["errors"]["code"]

        with app.app_context():
            assert db.session.get(Subject, second).code != taken
            assert db.session.get(Subject, first).code == taken

    def test_another_users_subject_is_not_editable(self, app, client):
        register(client, "first@example.com")
        upload(client)
        with app.app_context():
            subject_id = db.session.query(Subject).first().id

        other = app.test_client()
        register(other, "second@example.com")
        assert other.put(f"/api/subjects/{subject_id}",
                         json={"code": "MINE"}).status_code == 404

    def test_the_page_drops_the_filler_and_names_the_link_for_the_page(self, loaded):
        html = loaded.get("/subjects").get_data(as_text=True)
        assert "Shown in tables" not in html
        assert "Blank uses the default" not in html
        assert "Every lecture" not in html
        assert "Overview" in html


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
            "subject_limit": "90", "overall_limit": "95"})

        other = app.test_client()
        register(other, "second@example.com")
        assert other.get("/api/dashboard").get_json()["overall"]["limit"] == 75

    def test_change_history_is_scoped_to_its_owner(self, app, client):
        """`LectureChange` has no user_id of its own, so the scope has to come
        from the lecture. Every caller happens to pass ids it fetched safely —
        this asserts the function doesn't rely on that."""
        from app.services import changes_for_lectures

        register(client, "first@example.com")
        upload(client)

        with app.app_context():
            owner = db.session.query(User).filter_by(email="first@example.com").one()
            lecture_ids = [l.id for l in db.session.query(LectureInstance).all()]
            assert lecture_ids

            stranger = User(email="nobody@example.com", username="nobody",
                            session_token="t-nobody")
            db.session.add(stranger)
            db.session.commit()

            assert changes_for_lectures(owner, lecture_ids) is not None
            assert changes_for_lectures(stranger, lecture_ids) == {}


def test_users_table_stays_clean(app, client):
    register(client)
    with app.app_context():
        assert db.session.query(User).count() == 1


class TestPredictions:
    """Guesses at unmarked lectures feed the real numbers, and a fresh report
    always overrules them."""

    @pytest.fixture()
    def loaded(self, client):
        register(client)
        upload(client)
        return client

    def _a_pending_lecture(self, app):
        with app.app_context():
            from app.services import UNKNOWN_STATUSES

            row = (
                db.session.query(LectureInstance)
                .filter(LectureInstance.status.in_(UNKNOWN_STATUSES))
                .filter_by(is_vanished=False)
                .first()
            )
            return row.id, row.subject_id

    def test_a_guess_moves_the_lecture_between_buckets(self, loaded, app):
        lecture_id, subject_id = self._a_pending_lecture(app)

        before = loaded.get("/api/dashboard").get_json()
        subject_before = next(s for s in before["subjects"] if s["id"] == subject_id)

        resp = loaded.put(f"/api/lectures/{lecture_id}/prediction",
                          json={"predicted": "P"})
        assert resp.status_code == 200

        after = loaded.get("/api/dashboard").get_json()
        subject_after = next(s for s in after["subjects"] if s["id"] == subject_id)

        assert subject_after["pending"] == subject_before["pending"] - 1
        assert subject_after["worst_pct"] > subject_before["worst_pct"]

    def test_guessing_absent_never_flatters_the_worst_case(self, loaded, app):
        """Worst case already assumes absent, so an 'A' guess must not improve
        anything — if it did, the arithmetic would be double-counting."""
        lecture_id, subject_id = self._a_pending_lecture(app)

        before = loaded.get("/api/dashboard").get_json()
        loaded.put(f"/api/lectures/{lecture_id}/prediction", json={"predicted": "A"})
        after = loaded.get("/api/dashboard").get_json()

        b = next(s for s in before["subjects"] if s["id"] == subject_id)
        a = next(s for s in after["subjects"] if s["id"] == subject_id)
        assert a["worst_pct"] == b["worst_pct"]
        assert a["can_miss"] <= b["can_miss"]

    def test_a_guess_can_be_taken_back(self, loaded, app):
        lecture_id, subject_id = self._a_pending_lecture(app)
        before = loaded.get("/api/dashboard").get_json()

        loaded.put(f"/api/lectures/{lecture_id}/prediction", json={"predicted": "P"})
        loaded.delete(f"/api/lectures/{lecture_id}/prediction")

        after = loaded.get("/api/dashboard").get_json()
        assert after["overall"]["worst_pct"] == before["overall"]["worst_pct"]

    def test_a_marked_lecture_cannot_be_guessed_at(self, loaded, app):
        """Overwriting fact with opinion is the one thing this must never do."""
        with app.app_context():
            marked = (
                db.session.query(LectureInstance).filter_by(status="P").first()
            )
            lecture_id = marked.id
        resp = loaded.put(f"/api/lectures/{lecture_id}/prediction",
                          json={"predicted": "A"})
        assert resp.status_code == 409

    def test_a_report_that_resolves_the_lecture_retires_the_guess(self, loaded, app):
        """The guess is read through a join on the status, so reality wins
        without anything having to delete it."""
        lecture_id, _ = self._a_pending_lecture(app)
        loaded.put(f"/api/lectures/{lecture_id}/prediction", json={"predicted": "P"})

        with app.app_context():
            from app.services import predictions_for
            from app.models import User

            user = db.session.get(User, 1)
            assert lecture_id in predictions_for(user)

            # The college marks it — as absent, the opposite of the guess.
            db.session.get(LectureInstance, lecture_id).status = "A"
            db.session.commit()
            assert lecture_id not in predictions_for(user)

    def test_only_your_own_lectures_can_be_guessed_at(self, app, client):
        register(client, "first@example.com")
        upload(client)
        with app.app_context():
            from app.services import UNKNOWN_STATUSES

            lecture_id = (
                db.session.query(LectureInstance)
                .filter(LectureInstance.status.in_(UNKNOWN_STATUSES))
                .first()
            ).id

        other = app.test_client()
        register(other, "second@example.com")
        resp = other.put(f"/api/lectures/{lecture_id}/prediction",
                         json={"predicted": "P"})
        assert resp.status_code == 404


class TestBulkPredictions:
    """Fifty pending lectures, answered in one tap rather than fifty.

    The per-lecture buttons were honest and unusable: a student two months into
    term has forty-odd NU rows, each needing a click and a page reload.
    """

    @pytest.fixture()
    def loaded(self, client):
        register(client)
        upload(client)
        return client

    def _pending(self, app, subject_id=None):
        with app.app_context():
            from app.services import UNKNOWN_STATUSES

            query = (
                db.session.query(LectureInstance)
                .filter(LectureInstance.status.in_(UNKNOWN_STATUSES))
                .filter_by(is_vanished=False)
            )
            if subject_id is not None:
                query = query.filter_by(subject_id=subject_id)
            return [row.id for row in query.all()]

    def test_one_tap_answers_every_pending_lecture_of_a_subject(self, loaded, app):
        before = loaded.get("/api/dashboard").get_json()
        subject = max(before["subjects"], key=lambda s: s["pending"])
        pending = len(self._pending(app, subject["id"]))

        resp = loaded.post(f"/api/subjects/{subject['id']}/predictions",
                           json={"predicted": "P"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["changed"] == pending

        after = next(s for s in body["stats"]["subjects"] if s["id"] == subject["id"])
        assert after["pending"] == 0
        assert after["worst_pct"] > subject["worst_pct"]
        assert after["guessed"] == pending

    def test_the_answer_carries_the_recomputed_figures(self, loaded, app):
        """The per-request memo caches counts, so a response built after the
        write without dropping it would hand back the numbers from before it."""
        before = loaded.get("/api/dashboard").get_json()["overall"]["worst_pct"]
        body = loaded.post("/api/predictions/bulk", json={"predicted": "P"}).get_json()

        assert body["stats"]["overall"]["worst_pct"] > before
        assert body["stats"]["overall"]["worst_pct"] == \
            loaded.get("/api/dashboard").get_json()["overall"]["worst_pct"]

    def test_the_global_form_covers_every_subject(self, loaded, app):
        body = loaded.post("/api/predictions/bulk", json={"predicted": "P"}).get_json()
        assert body["changed"] == len(self._pending(app))
        assert all(s["pending"] == 0 for s in body["stats"]["subjects"])

    def test_clearing_restores_the_untouched_worst_case(self, loaded):
        before = loaded.get("/api/dashboard").get_json()["overall"]["worst_pct"]
        loaded.post("/api/predictions/bulk", json={"predicted": "P"})
        loaded.post("/api/predictions/bulk", json={"predicted": None})

        assert loaded.get("/api/dashboard").get_json()["overall"]["worst_pct"] == before

    def test_undo_puts_every_guess_back_as_it_was(self, loaded, app):
        """The previous map is what makes a bulk action reversible in one step."""
        first = self._pending(app)[0]
        loaded.put(f"/api/lectures/{first}/prediction", json={"predicted": "A"})
        mixed = loaded.get("/api/dashboard").get_json()["overall"]

        body = loaded.post("/api/predictions/bulk", json={"predicted": "P"}).get_json()
        assert body["previous"][str(first)] == "A"

        loaded.post("/api/predictions/bulk", json={"lectures": body["previous"]})
        restored = loaded.get("/api/dashboard").get_json()["overall"]
        assert restored["worst_pct"] == mixed["worst_pct"]
        assert restored["can_miss"] == mixed["can_miss"]

    def test_a_marked_lecture_is_never_overwritten(self, loaded, app):
        """A guess must never sit on top of something the college has said."""
        with app.app_context():
            marked = (
                db.session.query(LectureInstance)
                .filter(LectureInstance.status == "P")
                .first()
            )
            marked_id = marked.id

        loaded.post("/api/predictions/bulk", json={"predicted": "A"})
        with app.app_context():
            from app.models import LecturePrediction

            assert db.session.query(LecturePrediction).filter_by(
                lecture_id=marked_id).count() == 0

    def test_a_vanished_lecture_is_left_alone(self, loaded, app):
        with app.app_context():
            from app.services import UNKNOWN_STATUSES

            row = (
                db.session.query(LectureInstance)
                .filter(LectureInstance.status.in_(UNKNOWN_STATUSES))
                .first()
            )
            row.is_vanished = True
            db.session.commit()
            vanished_id = row.id

        loaded.post("/api/predictions/bulk", json={"predicted": "P"})
        with app.app_context():
            from app.models import LecturePrediction

            assert db.session.query(LecturePrediction).filter_by(
                lecture_id=vanished_id).count() == 0

    def test_another_users_subject_is_not_found(self, loaded, app):
        with app.app_context():
            subject_id = db.session.query(Subject).first().id

        other = app.test_client()
        register(other, "second@example.com")
        resp = other.post(f"/api/subjects/{subject_id}/predictions",
                          json={"predicted": "P"})
        assert resp.status_code == 404

    def test_only_what_actually_moved_is_counted(self, loaded):
        """The count drives the wording and the "nothing to do" path, so it has
        to mean changes rather than lectures in scope — otherwise Clear on a
        subject with no guesses cheerfully reports fifty cleared."""
        first = loaded.post("/api/predictions/bulk", json={"predicted": "P"}).get_json()
        assert first["changed"] > 0

        again = loaded.post("/api/predictions/bulk", json={"predicted": "P"}).get_json()
        assert again["changed"] == 0
        assert again["previous"] == {}

        cleared = loaded.post("/api/predictions/bulk", json={"predicted": None}).get_json()
        assert cleared["changed"] == first["changed"]
        assert loaded.post("/api/predictions/bulk",
                           json={"predicted": None}).get_json()["changed"] == 0

    def test_a_nonsense_value_is_refused(self, loaded):
        assert loaded.post("/api/predictions/bulk",
                           json={"predicted": "maybe"}).status_code == 400
        assert loaded.post("/api/predictions/bulk",
                           json={"lectures": "nope"}).status_code == 400


class TestPercentageMovesOverHttp:
    """`_apply` reads each subject's percentage before ingesting and again
    after, inside one request. Anything cached for the life of that request has
    to be dropped across the write, or both readings are the same number and
    the upload page reports that nothing moved.

    Asserted over HTTP on purpose: called directly there is no request context,
    so the caches are inert and a broken one still looks fine.
    """

    def _report(self, tmp_path, name, statuses):
        path = tmp_path / name
        make_detailed_pdf(path, [
            ("CN", date(2026, 7, 16 + i), time(9, 0), time(10, 0), status)
            for i, status in enumerate(statuses)
        ])
        return path

    def test_a_resolved_pending_lecture_moves_the_number(self, client, tmp_path):
        register(client)
        first = self._report(tmp_path, "a.pdf", ["NU", "P"])
        second = self._report(tmp_path, "b.pdf", ["P", "P"])

        upload(client, first, "a.pdf")
        body = upload(client, second, "b.pdf").get_json()

        assert body["status"] == "merged"
        # 1-of-2 worst case becomes 2-of-2. Same number twice would mean the
        # "before" reading survived the merge.
        assert body["pct_moves"]["Cn"] == [50.0, 100.0]
