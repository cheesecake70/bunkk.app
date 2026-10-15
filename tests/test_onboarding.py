"""The welcome flow, the gate in front of it, and the detected timetable.

Drives a real account through the real PDF the way a new student would, with
the gate switched on (TestConfig leaves it off for every other suite).
"""
from datetime import date, timedelta
from pathlib import Path

import pytest
from conftest import google_sign_in

from app import create_app, db, planning
from app.models import Event, Semester, TimetableVersion, User

GOLDEN = Path(__file__).parent / "golden" / "detailed_jul_aug.pdf"


@pytest.fixture()
def app(tmp_path):
    app = create_app("config.TestConfig")
    app.config.update(UPLOAD_DIR=str(tmp_path / "uploads"), SETUP_GATE=True)
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


def upload(client):
    return client.post(
        "/api/reports",
        data={"report": (GOLDEN.open("rb"), "report.pdf")},
        content_type="multipart/form-data",
    )


def rules(client, *, subject="70", overall="75", end=None, guess=False):
    end = end if end is not None else (date.today() + timedelta(days=60)).isoformat()
    data = {"subject_limit": subject, "overall_limit": overall, "end_date": end}
    if guess:
        data["guess"] = "1"
    return client.post("/welcome/rules", data=data)


def user(app) -> User:
    return db.session.query(User).one()


def set_up(client):
    upload(client)
    rules(client)


class TestGate:
    def test_a_new_account_starts_at_the_upload_screen(self, client):
        resp = client.get("/")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/welcome")
        html = client.get("/welcome").get_data(as_text=True)
        assert 'id="dropzone"' in html
        assert 'class="rail"' in html

    @pytest.mark.parametrize("path", ["/plan", "/calendar", "/subjects", "/checkpoints"])
    def test_every_other_page_waits_for_setup(self, client, path):
        assert client.get(path).headers.get("Location", "").endswith("/welcome")

    @pytest.mark.parametrize("path", ["/upload", "/setup", "/settings", "/timetable"])
    def test_upload_setup_and_the_account_stay_reachable(self, client, path):
        """Nobody gets locked out of their own account settings — deleting it
        included — by a setup they haven't finished."""
        assert client.get(path).status_code == 200

    def test_the_api_is_not_gated(self, client):
        assert client.get("/api/dashboard").status_code == 200

    def test_signed_out_visitors_still_see_the_landing_page(self, app):
        assert app.test_client().get("/").status_code == 200

    def test_an_existing_account_is_walked_through_once(self, client, app):
        """Accounts from before the welcome flow have a report, maybe even an
        end date — but never chose their limits, so they land on that screen
        with the end date they already have filled in."""
        upload(client)
        end = date.today() + timedelta(days=60)
        with app.app_context():
            planning.set_semester_end(user(app), end)
        html = client.get("/welcome").get_data(as_text=True)
        assert 'name="subject_limit"' in html
        assert f'value="{end.isoformat()}"' in html
        assert client.get("/").status_code == 302

        rules(client, end=end.isoformat())
        assert client.get("/").status_code == 200

    def test_a_new_semester_asks_for_its_end_date_again(self, client, app):
        set_up(client)
        with app.app_context():
            db.session.query(Semester).one().end_date = None
            db.session.commit()
        assert client.get("/").headers["Location"].endswith("/welcome")
        html = client.get("/welcome").get_data(as_text=True)
        # The limits were chosen once; they come back filled in, not blank.
        assert 'value="70"' in html and 'value="75"' in html


class TestRulesScreen:
    def test_limits_start_empty_with_the_usual_numbers_as_hints(self, client):
        upload(client)
        html = client.get("/welcome").get_data(as_text=True)
        assert 'placeholder="70"' in html and 'placeholder="75"' in html
        assert 'name="subject_limit" inputmode="numeric" maxlength="3" placeholder="70"\n' \
               '                   value=""' in html

    def test_saving_records_the_limits_and_the_end_date(self, client, app):
        upload(client)
        end = date.today() + timedelta(days=60)
        resp = rules(client, subject="75", overall="80", end=end.isoformat())
        assert resp.headers["Location"].endswith("/welcome?step=exams")
        with app.app_context():
            u = user(app)
            assert u.setup_done_at is not None
            settings = planning.settings_for(u)
            assert (settings.subject_limit, settings.overall_limit) == (75, 80)
            semester = db.session.query(Semester).one()
            assert semester.end_date == end and not semester.end_date_is_guess
            assert db.session.query(Event).filter_by(name="limits_chosen").count() == 1

    @pytest.mark.parametrize("field", ["subject", "overall", "end"])
    def test_every_box_is_required(self, client, app, field):
        upload(client)
        resp = rules(client, **{field: ""})
        assert resp.status_code == 400
        with app.app_context():
            assert user(app).setup_done_at is None

    def test_nothing_is_half_saved(self, client, app):
        """A bad date must not leave the limits stored and the screen 'done'."""
        upload(client)
        resp = rules(client, subject="60", overall="65", end="2001-01-01")
        assert resp.status_code == 400
        assert "in the future" in resp.get_data(as_text=True)
        with app.app_context():
            assert planning.settings_for(user(app)).subject_limit == 70

    def test_a_bad_percentage_is_explained(self, client):
        upload(client)
        resp = rules(client, overall="120")
        assert resp.status_code == 400
        assert "between 0 and 100" in resp.get_data(as_text=True)


class TestExamsScreen:
    def test_it_comes_last_and_one_tap_finishes(self, client):
        set_up(client)
        html = client.get("/welcome?step=exams").get_data(as_text=True)
        assert "Nope →" in html
        resp = client.post("/welcome/finish")
        assert resp.headers["Location"].endswith("/")
        assert "all set" in client.get("/").get_data(as_text=True)

    def test_a_checkpoint_added_here_stays_here(self, client):
        set_up(client)
        on = date.today() + timedelta(days=20)
        resp = client.post("/checkpoints", data={
            "on_date": on.isoformat(), "label": "Mid-sems", "back": "welcome",
        })
        assert resp.headers["Location"].endswith("/welcome?step=exams")
        html = client.get("/welcome?step=exams").get_data(as_text=True)
        assert "Mid-sems" in html
        assert "Done →" in html

    def test_once_set_up_welcome_has_nothing_left_to_ask(self, client):
        set_up(client)
        assert client.get("/welcome").headers["Location"].endswith("/")


class TestRail:
    def test_it_fills_as_you_go(self, client):
        def states(html):
            return [s for s in ("is-done", "is-now", "is-todo") for _ in range(html.count(f"rail__seg {s}"))]

        assert states(client.get("/welcome").get_data(as_text=True)) == ["is-now", "is-todo", "is-todo"]
        upload(client)
        assert states(client.get("/welcome").get_data(as_text=True)) == ["is-done", "is-now", "is-todo"]
        rules(client)
        assert states(client.get("/welcome?step=exams").get_data(as_text=True)) == ["is-done", "is-done", "is-now"]


class TestRoughEndDate:
    def test_a_guess_still_turns_planning_on(self, client, app):
        upload(client)
        rules(client, guess=True)
        with app.app_context():
            assert db.session.query(Semester).one().end_date_is_guess
        html = client.get("/").get_data(as_text=True)
        assert "is a guess" in html
        assert 'class="nav__dot"' in html

    def test_the_suggestion_is_in_the_future(self, client, app):
        upload(client)
        with app.app_context():
            assert planning.suggested_semester_end(user(app)) > date.today()
        assert "Guess for me" in client.get("/welcome").get_data(as_text=True)

    def test_saving_a_real_date_clears_the_guess(self, client, app):
        upload(client)
        rules(client, guess=True)
        end = (date.today() + timedelta(days=50)).isoformat()
        resp = client.post("/calendar/end-date", data={"end_date": end, "back": "setup"})
        assert resp.headers["Location"].endswith("/setup#semester-end")
        with app.app_context():
            assert not db.session.query(Semester).one().end_date_is_guess
        assert "is a guess" not in client.get("/").get_data(as_text=True)


class TestDetectedTimetable:
    def test_an_upload_draws_the_timetable_itself(self, client, app):
        upload(client)
        with app.app_context():
            version = db.session.query(TimetableVersion).one()
            assert version.source == "inferred"
            assert planning.has_timetable(user(app))

    def test_a_second_identical_upload_mints_no_new_version(self, client, app):
        upload(client)
        with app.app_context():
            assert planning.adopt_detected_timetable(user(app)) is False
            assert db.session.query(TimetableVersion).count() == 1

    def test_a_grid_you_edited_is_never_overwritten(self, client, app):
        upload(client)
        with app.app_context():
            u = user(app)
            subject_id = planning.subjects_for(u)[0].id
            planning.save_timetable(u, planning.entries_from([{
                "kind": "class", "weekday": "0", "start": "09:00", "end": "10:00",
                "subject_id": str(subject_id),
            }]), source="manual")
            # The same day: the hand edit took the detected version over.
            assert db.session.query(TimetableVersion).one().source == "manual"
            assert planning.adopt_detected_timetable(u) is False
            assert len(planning.active_slots(u)) == 1

    def test_the_setup_page_says_where_the_grid_came_from(self, client):
        set_up(client)
        html = client.get("/setup").get_data(as_text=True)
        assert "detected from your reports" in html
