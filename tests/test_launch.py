"""The launch pass: what breaks when the caller isn't one of our own pages.

Every page sends well-formed requests, so none of this shows up by clicking
around. It shows up the first time a stranger, a stale tab or a script sends
something else — and the difference between a 400 and a 500 is whether the
app said "no" or fell over.
"""
import io
from datetime import date, time, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import google_sign_in
from reportlab_stub import make_detailed_pdf
from sqlalchemy.exc import OperationalError

import config
from app import create_app, db
from app.models import ReportSnapshot, Semester, Subject, User

GOLDEN = Path(__file__).parent / "golden" / "detailed_jul_aug.pdf"


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
def client(app):
    client = app.test_client()
    google_sign_in(client, "m@example.com", "muser")
    return client


def upload(client, data: bytes, filename="report.pdf"):
    return client.post("/api/reports",
                       data={"report": (io.BytesIO(data), filename)},
                       content_type="multipart/form-data")


def tomorrow() -> str:
    return (date.today() + timedelta(days=1)).isoformat()


@pytest.fixture()
def planned(app, client):
    """A signed-in student with a ledger, a timetable and a term end."""
    assert upload(client, GOLDEN.read_bytes()).status_code == 200
    with app.app_context():
        subject_id = db.session.query(Subject).first().id
    blocks = [{"kind": "class", "weekday": d, "start": "09:00", "end": "10:00",
               "subject_id": subject_id} for d in range(7)]
    assert client.put("/api/timetable", json={"blocks": blocks}).status_code == 200
    end = (date.today() + timedelta(days=60)).isoformat()
    assert client.post("/calendar/end-date", data={"end_date": end}).status_code == 302
    return subject_id


# ---------------------------------------------------------------------------
# Malformed requests
# ---------------------------------------------------------------------------

#: Things JSON can be that no page ever sends.
JUNK = [None, 1, "x", [], [1], {"a": 1}, True, 10 ** 30, "9999-12-31", "\x00"]


class TestMalformedRequests:
    @pytest.mark.parametrize("junk", JUNK)
    def test_no_endpoint_falls_over(self, client, planned, junk):
        sid = planned
        day = tomorrow()
        requests = [
            ("post", "/api/calendar/day", junk),
            ("post", "/api/calendar/day", {"date": junk}),
            ("post", "/api/calendar/day", {"date": day, "kind": junk}),
            ("post", "/api/calendar/day", {"date": day, "kind": "holiday", "name": junk}),
            ("post", "/api/calendar/range", junk),
            ("post", "/api/calendar/range", {"from": junk, "to": day}),
            ("post", "/api/calendar/range", {"from": day, "to": day, "name": junk}),
            ("post", "/api/absences", junk),
            ("post", "/api/absences", {"date": junk}),
            ("post", "/api/absences", {"date": day, "subject_id": junk}),
            ("post", "/api/absences", {"date": day, "subject_id": sid, "start": junk}),
            ("post", "/api/absences", {"date": day, "note": junk}),
            ("post", "/api/absences/batch", junk),
            ("post", "/api/absences/batch", {"date": day, "lectures": junk}),
            ("post", "/api/absences/batch", {"date": day, "lectures": [junk]}),
            ("post", "/api/absences/batch",
             {"date": day, "lectures": [{"subject_id": junk, "start": junk}]}),
            ("post", "/api/simulate", junk),
            ("post", "/api/simulate", {"absences": junk}),
            ("post", "/api/simulate", {"absences": [junk]}),
            ("post", "/api/simulate", {"absences": [{"date": junk}]}),
            ("post", "/api/simulate",
             {"absences": [{"date": day, "subject_id": junk, "start": junk}]}),
            ("put", "/api/timetable", junk),
            ("put", "/api/timetable", {"blocks": junk}),
            ("put", "/api/timetable", {"blocks": [junk]}),
            ("put", "/api/timetable", {"blocks": [
                {"kind": "class", "weekday": junk, "start": junk, "end": junk,
                 "subject_id": junk, "label": junk}]}),
            ("put", "/api/timetable", {"blocks": [
                {"kind": "class", "weekday": 0, "start": "09:00", "end": "10:00",
                 "subject_id": sid},
                {"kind": "break", "weekday": 0, "start": "10:00", "end": "11:00",
                 "label": junk}]}),
            ("put", f"/api/subjects/{sid}", junk),
            ("put", f"/api/subjects/{sid}",
             {"name": junk, "code": junk, "custom_limit": junk}),
            ("post", f"/api/subjects/{sid}/predictions", junk),
            ("post", f"/api/subjects/{sid}/predictions", {"predicted": junk}),
            ("post", "/api/predictions/bulk", junk),
            ("post", "/api/predictions/bulk", {"lectures": junk}),
            ("post", "/api/predictions/bulk", {"lectures": {"1": junk}}),
            ("post", "/api/predictions/bulk", {"lectures": {str(junk): "P"}}),
            ("post", "/api/predictions/bulk", {"predicted": junk}),
            ("put", "/api/lectures/1/prediction", junk),
            ("put", "/api/lectures/1/prediction", {"predicted": junk}),
            ("post", "/api/reports/1/resolve", junk),
            ("post", "/api/reports/1/resolve", {"decisions": junk}),
            ("post", "/api/reports/1/resolve", {"decisions": {"x": junk}}),
        ]
        for method, url, body in requests:
            # TestConfig re-raises anything unhandled, so reaching the
            # assertion at all is most of the test.
            resp = getattr(client, method)(url, json=body)
            assert resp.status_code < 500, (method, url, body)
            assert resp.is_json, (method, url, body)

    def test_a_body_that_is_not_json_at_all(self, client, planned):
        resp = client.post("/api/absences", data="not json",
                           content_type="application/json")
        assert resp.status_code == 400
        assert resp.get_json()["error"]

    def test_lists_have_a_ceiling(self, client, planned):
        sid = planned
        day = tomorrow()
        lecture = {"subject_id": sid, "start": "09:00"}
        assert client.post("/api/absences/batch",
                           json={"date": day, "lectures": [lecture] * 51}).status_code == 400
        assert client.post("/api/simulate", json={
            "absences": [{"date": day}] * 501}).status_code == 400
        assert client.post("/api/predictions/bulk", json={
            "lectures": {str(i): "P" for i in range(1, 5002)}}).status_code == 400
        block = {"kind": "class", "weekday": 0, "start": "09:00", "end": "10:00",
                 "subject_id": sid}
        assert client.put("/api/timetable",
                          json={"blocks": [block] * 301}).status_code == 400

    def test_free_text_is_trimmed_to_its_column(self, app, client, planned):
        from app.models import Holiday, PlannedAbsence
        day = tomorrow()
        assert client.post("/api/absences",
                           json={"date": day, "note": "n" * 999}).status_code == 200
        assert client.post("/api/calendar/day", json={
            "date": (date.today() + timedelta(days=2)).isoformat(),
            "kind": "holiday", "name": "h" * 999}).status_code == 200
        with app.app_context():
            assert len(db.session.query(PlannedAbsence).one().note) == 200
            assert len(db.session.query(Holiday).one().name) == 120

    def test_a_limit_of_zero_is_a_limit(self, app, client, planned):
        resp = client.put(f"/api/subjects/{planned}", json={"custom_limit": 0})
        assert resp.status_code == 200
        assert resp.get_json()["subject"]["custom_limit"] == 0

    def test_dates_at_the_edge_of_the_calendar(self, client, planned):
        # date.max + 1 day is an OverflowError; these used to be a 500.
        assert client.get("/api/day/9999-12-31").status_code == 400
        assert client.get("/api/day/0001-01-01").status_code == 400
        assert client.get("/?date=9999-12-31").status_code == 200
        assert client.get("/?date=0001-01-01").status_code == 200


# ---------------------------------------------------------------------------
# A mistyped year
# ---------------------------------------------------------------------------


class TestFarFuture:
    def test_a_semester_cannot_end_centuries_away(self, app, client, planned):
        resp = client.post("/calendar/end-date", data={"end_date": "9999-12-31"})
        assert resp.status_code == 302 and "/calendar" in resp.headers["Location"]
        with app.app_context():
            end = db.session.query(Semester).one().end_date
        assert end == date.today() + timedelta(days=60)
        # The pages that project out to the end still render.
        for url in ("/", "/plan", "/calendar"):
            assert client.get(url).status_code == 200

    def test_nor_can_a_checkpoint(self, client):
        assert upload(client, GOLDEN.read_bytes()).status_code == 200
        resp = client.post("/checkpoints", data={"on_date": "9999-12-31"})
        assert resp.status_code == 400
        assert "more than a year away" in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------


def week(path, name, day=13):
    make_detailed_pdf(
        path,
        [(name, date(2026, 8, day), time(10, 0, 1), time(11, 0, 0), "P")],
        period_start=date(2026, 8, 13), period_end=date(2026, 8, 20),
    )
    return path.read_bytes()


class TestStagedUploads:
    def test_an_abandoned_question_is_asked_again(self, app, client, tmp_path):
        """Close the tab at "is this the same subject?" and upload the same
        file later: it must ask again, not claim there is nothing new."""
        upload(client, week(tmp_path / "w1.pdf", "Computer NetworksT C2"))
        renamed = week(tmp_path / "w2.pdf", "Computer NetworkT C2", day=14)

        first = upload(client, renamed).get_json()
        assert first["status"] == "needs_confirmation"

        again = upload(client, renamed).get_json()
        assert again["status"] == "needs_confirmation"
        assert again["snapshot_id"] == first["snapshot_id"]
        assert again["proposals"] == first["proposals"]

        raw = first["proposals"][0]["raw_name"]
        done = client.post(f"/api/reports/{first['snapshot_id']}/resolve",
                           json={"decisions": {raw: "new"}}).get_json()
        assert done["status"] == "merged" and done["added"] == 1

        # And once it has merged, the same file really is nothing new.
        assert upload(client, renamed).get_json()["status"] == "duplicate"
        with app.app_context():
            assert db.session.query(ReportSnapshot).count() == 2

    def test_an_answer_naming_nothing_real_is_refused(self, client, tmp_path):
        upload(client, week(tmp_path / "w1.pdf", "Computer NetworksT C2"))
        staged = upload(client, week(tmp_path / "w2.pdf", "Computer NetworkT C2",
                                     day=14)).get_json()
        raw = staged["proposals"][0]["raw_name"]
        for answer in ("merge:", "merge:abc", "merge:999999", f"merge:{10 ** 30}"):
            resp = client.post(f"/api/reports/{staged['snapshot_id']}/resolve",
                               json={"decisions": {raw: answer}})
            assert resp.status_code == 422, answer

    def test_two_uploads_of_one_file_never_share_a_path(self, app, client, tmp_path):
        from app.merge import _store_pdf
        with app.app_context():
            user = db.session.query(User).one()
            first = _store_pdf(user, b"%PDF same", "d" * 64, "r.pdf")
            second = _store_pdf(user, b"%PDF same", "d" * 64, "r.pdf")
        assert first != second
        assert Path(first).exists() and Path(second).exists()

    def test_a_failed_merge_leaves_no_pdf_behind(self, app, client, tmp_path):
        with patch("app.merge._merge_or_ask", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                upload(client, week(tmp_path / "w1.pdf", "Computer NetworksT C2"))
        stored = list(Path(app.config["UPLOAD_DIR"]).rglob("*.pdf"))
        assert stored == []


class LimitedConfig(config.TestConfig):
    RATELIMIT_ENABLED = True


class TestUploadThrottle:
    def test_uploads_are_metered_per_account_not_per_address(self, tmp_path):
        """A hostel shares one address, so the meter is on the account: one
        student hammering the parser must not lock out the corridor."""
        app = create_app(LimitedConfig)
        app.config.update(UPLOAD_DIR=str(tmp_path / "uploads"))
        with app.app_context():
            db.create_all()
        try:
            noisy = app.test_client()
            google_sign_in(noisy, "noisy@example.com")
            codes = [upload(noisy, b"not a pdf").status_code for _ in range(21)]
            assert codes[:20] == [422] * 20
            assert codes[20] == 429
            assert upload(noisy, b"not a pdf").get_json()["error"]

            neighbour = app.test_client()
            google_sign_in(neighbour, "quiet@example.com")
            assert upload(neighbour, b"not a pdf").status_code == 422
        finally:
            with app.app_context():
                db.session.remove()
                db.drop_all()
            from app import limiter
            limiter.reset()


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------


class TestSignIn:
    def test_a_reassigned_address_does_not_inherit_the_account(self, app):
        """A college hands last year's mailbox to a new student. Same address,
        different Google identity — and not the same person."""
        first = app.test_client()
        google_sign_in(first, "roll42@college.example", sub="google-old")

        second = app.test_client()
        resp = google_sign_in(second, "roll42@college.example", sub="google-new")
        assert resp.status_code == 302 and "/login" in resp.headers["Location"]
        assert second.get("/settings").status_code == 302     # not signed in

        with app.app_context():
            assert db.session.query(User).one().google_sub == "google-old"

    def test_a_rename_onto_a_taken_address_keeps_the_old_one(self, app):
        google_sign_in(app.test_client(), "a@example.com", sub="sub-a")
        google_sign_in(app.test_client(), "b@example.com", sub="sub-b")

        client = app.test_client()
        resp = google_sign_in(client, "a@example.com", sub="sub-b")
        assert resp.status_code == 302
        with app.app_context():
            emails = {u.google_sub: u.email for u in db.session.query(User)}
        assert emails == {"sub-a": "a@example.com", "sub-b": "b@example.com"}

    def test_two_first_sign_ins_at_once_make_one_account(self, app):
        """The second request loses the race on the unique index and must find
        the account the first one made, rather than answer 500."""
        from app import auth

        real = auth._find_or_create_user
        calls = []

        def racing(sub, email):
            if not calls:
                calls.append(1)
                real(sub, email)                 # the other request wins...
                db.session.add(User(email=email, google_sub=sub, username="dup"))
                db.session.flush()               # ...and this one collides
            return real(sub, email)

        with patch("app.auth._find_or_create_user", side_effect=racing):
            resp = google_sign_in(app.test_client(), "race@example.com")
        assert resp.status_code == 302
        with app.app_context():
            assert db.session.query(User).count() == 1

    def test_dev_login_will_not_take_over_a_google_account(self, app):
        google_sign_in(app.test_client(), "real@example.com", sub="google-real")
        app.debug = True
        app.config["DEV_LOGIN"] = True
        resp = app.test_client().post("/login/dev", data={"email": "real@example.com"})
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/login/dev")
        with app.app_context():
            assert db.session.query(User).one().google_sub == "google-real"

    def test_google_being_unreachable_is_a_sentence_not_a_500(self, app):
        with patch(
            "authlib.integrations.flask_client.apps.FlaskOAuth2App.authorize_redirect",
            side_effect=ConnectionError("no route to host"),
        ):
            resp = app.test_client().get("/login/google")
        assert resp.status_code == 302 and "/login" in resp.headers["Location"]

    def test_calls_to_google_carry_a_timeout(self, app):
        from app.auth import GOOGLE_TIMEOUT, OAUTH_EXT
        google = app.extensions[OAUTH_EXT].google
        assert google.client_kwargs["default_timeout"] == GOOGLE_TIMEOUT

    @pytest.mark.parametrize("target", ["/\\evil.example", "/ok\\x", "/a\nb", "//evil.example",
                                        "https://evil.example"])
    def test_next_cannot_be_dressed_up_as_a_path(self, app, target):
        client = app.test_client()
        google_sign_in(client, "m@example.com")
        resp = client.post("/logout", data={"next": target})
        assert resp.headers["Location"].endswith("/login")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestConfiguration:
    def test_a_relative_sqlite_path_is_anchored_to_the_repo(self):
        # Flask-SQLAlchemy resolves a relative path against instance/, which
        # turned the documented value into instance/instance/bunkk.db.
        url = config._database_url("sqlite:///instance/bunkk.db")
        assert url == "sqlite:///" + str(Path(config.BASEDIR) / "instance" / "bunkk.db")

    @pytest.mark.parametrize("url", [
        "sqlite://", "sqlite:///:memory:", "sqlite:////var/lib/bunkk/bunkk.db",
        "postgresql://u:p@db/bunkk", "",
    ])
    def test_other_database_urls_pass_through(self, url):
        assert config._database_url(url) == url

    def test_the_scheme_hosting_platforms_hand_out_is_accepted(self):
        assert config._database_url("postgres://u:p@db/bunkk") == "postgresql://u:p@db/bunkk"

    def test_the_upload_directory_does_not_depend_on_the_working_directory(self):
        assert config._path("instance/uploads") == str(
            Path(config.BASEDIR) / "instance" / "uploads")
        assert config._path("/srv/bunkk/uploads") == "/srv/bunkk/uploads"

    def test_production_never_offers_dev_login(self, monkeypatch):
        import importlib
        monkeypatch.setenv("BUNKK_DEV_LOGIN", "1")
        importlib.reload(config)
        try:
            assert config.ProdConfig.DEV_LOGIN is False
            assert config.DevConfig.DEV_LOGIN is True
        finally:
            monkeypatch.undo()
            importlib.reload(config)

    @pytest.mark.parametrize("given", [None, ""])
    def test_gunicorn_runs_production_unless_told_otherwise(self, monkeypatch, given):
        """Unset, or blank as `.env.example` leaves it: both mean production."""
        import os
        import runpy
        if given is None:
            monkeypatch.delenv("BUNKK_CONFIG", raising=False)
        else:
            monkeypatch.setenv("BUNKK_CONFIG", given)
        with patch("dotenv.load_dotenv"):
            runpy.run_path(str(Path(config.BASEDIR) / "gunicorn.conf.py"))
            assert os.environ["BUNKK_CONFIG"] == "config.ProdConfig"
        monkeypatch.delenv("BUNKK_CONFIG", raising=False)

    def test_gunicorn_says_so_when_told_to_serve_debug(self, monkeypatch, capsys):
        import runpy
        monkeypatch.setenv("BUNKK_CONFIG", "config.DevConfig")
        with patch("dotenv.load_dotenv"):
            runpy.run_path(str(Path(config.BASEDIR) / "gunicorn.conf.py"))
        assert "not config.ProdConfig" in capsys.readouterr().err

    def test_the_example_env_file_does_not_choose_a_config(self):
        """Copying .env.example and starting gunicorn must give production;
        a value here would pre-empt gunicorn's default."""
        lines = (Path(config.BASEDIR) / ".env.example").read_text().splitlines()
        assert "BUNKK_CONFIG=" in lines

    def test_a_blank_config_name_means_the_default(self, monkeypatch):
        monkeypatch.setenv("BUNKK_CONFIG", "")
        assert create_app().config["DEBUG"] is True


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    def test_healthy_means_the_database_answers(self, app):
        assert app.test_client().get("/healthz").get_json() == {"status": "ok", "app": "bunkk"}

    def test_an_unreachable_database_is_not_ok(self, app):
        boom = OperationalError("SELECT 1", {}, Exception("unable to open database file"))
        with patch("app.routes.db.session.execute", side_effect=boom):
            resp = app.test_client().get("/healthz")
        assert resp.status_code == 503
        assert resp.get_json()["status"] == "error"


# ---------------------------------------------------------------------------
# Two requests at once
# ---------------------------------------------------------------------------


class TestClashes:
    """A double tap, a retry on a bad connection, a second tab: the same
    request twice, close enough together that both pass the "is it there
    yet?" check."""

    def test_the_database_refuses_a_second_whole_day(self, app, client, planned):
        from sqlalchemy.exc import IntegrityError
        from app.models import PlannedAbsence
        day = date.today() + timedelta(days=5)
        with app.app_context():
            user_id = db.session.query(User).one().id
            for subject_id in (None, planned):
                db.session.add(PlannedAbsence(user_id=user_id, on_date=day,
                                              subject_id=subject_id))
                db.session.commit()
                # NULLs are distinct to the plain unique constraint; only the
                # partial indexes stop these.
                db.session.add(PlannedAbsence(user_id=user_id, on_date=day,
                                              subject_id=subject_id))
                with pytest.raises(IntegrityError):
                    db.session.commit()
                db.session.rollback()

    def test_the_loser_of_a_race_gets_the_winners_answer(self, tmp_path):
        import threading

        class FileBacked(config.TestConfig):
            # In-memory SQLite is one connection shared by every thread;
            # a race needs a real file and a connection each.
            SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'race.db'}"

        app = create_app(FileBacked)
        app.config.update(UPLOAD_DIR=str(tmp_path / "uploads"))
        with app.app_context():
            db.create_all()
        setup = app.test_client()
        google_sign_in(setup, "m@example.com", "muser")
        assert upload(setup, GOLDEN.read_bytes()).status_code == 200
        with app.app_context():
            sid = db.session.query(Subject).first().id
        blocks = [{"kind": "class", "weekday": d, "start": "09:00", "end": "10:00",
                   "subject_id": sid} for d in range(7)]
        setup.put("/api/timetable", json={"blocks": blocks})
        setup.post("/calendar/end-date",
                   data={"end_date": (date.today() + timedelta(days=60)).isoformat()})

        def all_at_once(url, body, n=8):
            codes = []

            def go():
                one = app.test_client()
                google_sign_in(one, "m@example.com")
                codes.append(one.post(url, json=body).status_code)

            threads = [threading.Thread(target=go) for _ in range(n)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            return codes

        day = (date.today() + timedelta(days=3)).isoformat()
        try:
            assert set(all_at_once("/api/absences", {"date": day})) == {200}
            assert set(all_at_once("/api/absences",
                                   {"date": day, "subject_id": sid})) == {200}
            assert set(all_at_once("/api/calendar/day", {
                "date": (date.today() + timedelta(days=4)).isoformat(),
                "kind": "holiday"})) == {200}
            assert set(all_at_once("/api/predictions/bulk", {"predicted": "P"})) == {200}
            with app.app_context():
                from app.models import Holiday, PlannedAbsence
                assert db.session.query(PlannedAbsence).count() == 2
                assert db.session.query(Holiday).count() == 1
        finally:
            with app.app_context():
                db.session.remove()
                db.drop_all()
                db.engine.dispose()

    def test_a_whole_day_never_stores_a_start_time(self, app, client, planned):
        from app.models import PlannedAbsence
        day = (date.today() + timedelta(days=3)).isoformat()
        assert client.post("/api/absences",
                           json={"date": day, "start": "09:00"}).status_code == 200
        with app.app_context():
            assert db.session.query(PlannedAbsence).one().start_time is None

    def test_a_username_taken_mid_request_is_a_sentence(self, app):
        google_sign_in(app.test_client(), "a@example.com", "taken")
        client = app.test_client()
        google_sign_in(client, "b@example.com", "other")
        # The check passes (as it would a moment before the other commit)...
        with patch("app.account.validate_username", return_value=None):
            resp = client.post("/account/profile", data={"username": "taken"})
        # ...and the unique index has the last word.
        assert resp.status_code == 302
        page = client.get("/settings").get_data(as_text=True)
        assert "That username is taken." in page

    def test_any_other_clash_is_a_409_not_a_500(self, app, client, planned):
        from sqlalchemy.exc import IntegrityError
        boom = IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))
        with patch("app.planning.save_timetable", side_effect=boom):
            resp = client.put("/api/timetable", json={"blocks": [
                {"kind": "class", "weekday": 0, "start": "09:00", "end": "10:00",
                 "subject_id": planned}]})
        assert resp.status_code == 409
        assert "clashed" in resp.get_json()["error"]


class TestUrls:
    @pytest.mark.parametrize("method,url", [
        ("get", "/subjects/{n}"),
        ("put", "/api/subjects/{n}"),
        ("get", "/api/subjects/{n}/skip-ladder"),
        ("post", "/api/subjects/{n}/predictions"),
        ("put", "/api/lectures/{n}/prediction"),
        ("delete", "/api/absences/{n}"),
        ("post", "/api/reports/{n}/resolve"),
        ("post", "/checkpoints/{n}/delete"),
    ])
    def test_an_id_no_database_could_hold_is_just_not_found(self, client, method, url):
        # SQLite cannot bind an integer this size; it used to be a 500.
        resp = getattr(client, method)(url.format(n=10 ** 30), json={})
        assert resp.status_code == 404


class TestCaching:
    def test_a_students_pages_are_never_stored_by_the_browser(self, client):
        """Sign out on a shared laptop, press Back: the page must not come
        out of the cache."""
        assert client.get("/settings").headers["Cache-Control"] == "no-store"
        assert client.get("/api/dashboard").headers["Cache-Control"] == "no-store"

    def test_static_files_keep_their_ordinary_caching(self, client):
        resp = client.get("/static/css/app.css")
        assert resp.status_code == 200
        assert "no-store" not in resp.headers.get("Cache-Control", "")


class TestSchema:
    def test_the_migrations_build_exactly_what_the_models_describe(self, tmp_path):
        """`flask db check`: a model change with no migration behind it works
        on the laptop that ran create_all and breaks on the server."""
        from flask_migrate import check, upgrade

        class Migrated(config.TestConfig):
            SQLALCHEMY_DATABASE_URI = f"sqlite:///{tmp_path / 'migrated.db'}"

        app = create_app(Migrated)
        with app.app_context():
            upgrade()
            try:
                check()                    # raises SystemExit/CommandError on drift
            finally:
                db.session.remove()
                db.engine.dispose()


class TestTimezone:
    def test_today_is_the_colleges_today(self, monkeypatch):
        import importlib
        import time as clock
        monkeypatch.setenv("BUNKK_TIMEZONE", "Asia/Kolkata")
        monkeypatch.setenv("TZ", "UTC")
        importlib.reload(config)
        try:
            assert clock.localtime().tm_gmtoff == 5.5 * 3600
        finally:
            monkeypatch.undo()
            importlib.reload(config)

    def test_a_misspelt_zone_stops_the_boot(self, monkeypatch):
        monkeypatch.setenv("BUNKK_TIMEZONE", "Asia/Mumbay")
        with pytest.raises(RuntimeError, match="BUNKK_TIMEZONE"):
            config._set_timezone()


class TestSitemap:
    def test_lists_only_the_public_pages(self, app):
        resp = app.test_client().get("/sitemap.xml")
        assert resp.status_code == 200
        assert resp.mimetype == "application/xml"
        body = resp.get_data(as_text=True)
        for path in ("/", "/register", "/login"):
            assert f"<loc>http://localhost{path}</loc>" in body
        # Everything else is one student's page behind sign-in.
        assert body.count("<loc>") == 3
        assert "/upload" not in body and "/healthz" not in body
