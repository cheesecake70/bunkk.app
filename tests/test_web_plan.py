"""Phase 2 web layer: timetable confirmation, calendar, wallet, day strip.

The fixture drives the whole advanced-mode setup the way a user would: upload
the real PDF, accept the inferred timetable, set the semester end date.
"""
from datetime import date, timedelta
from pathlib import Path

import pytest

from app import create_app, db
from app.models import Holiday, PlannedAbsence, Subject, TimetableSlot

GOLDEN = Path(__file__).parent / "golden" / "detailed_jul_aug.pdf"

#: A fixed "today" inside the report window keeps every assertion deterministic.
TODAY = date(2026, 8, 16)
SEMESTER_END = date(2026, 11, 28)


@pytest.fixture()
def app(tmp_path, monkeypatch):
    app = create_app("config.TestConfig")
    app.config.update(UPLOAD_DIR=str(tmp_path / "uploads"))
    with app.app_context():
        db.create_all()
    yield app
    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app, monkeypatch):
    import app.api as api_module
    import app.planning as planning_module

    # Freeze "today" everywhere a request can ask for it. The API modules need
    # this as much as the planning layer: their "that day has already happened"
    # guards call date.today() themselves, so patching only planning left these
    # tests passing purely because the wall clock happened to sit near TODAY.
    class FrozenDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(planning_module, "date", FrozenDate)
    monkeypatch.setattr(api_module, "date", FrozenDate)

    client = app.test_client()
    client.post("/register", data={"email": "m@example.com", "password": "password123", "username": "muser", "confirm_password": "password123"})
    client.post(
        "/api/reports",
        data={"report": (GOLDEN.open("rb"), "report.pdf")},
        content_type="multipart/form-data",
    )
    return client


def confirm_timetable(client):
    """Accept every confident inferred slot, as the UI's default state would."""
    page = client.get("/timetable").get_data(as_text=True)
    import re

    tokens = re.findall(r'name="slot"\s+value="([^"]+)"\s+checked', page)
    assert tokens, "expected pre-checked slots on the timetable page"
    return client.post("/timetable", data={"slot": tokens}, follow_redirects=False), tokens


def set_end(client, end=SEMESTER_END):
    return client.post("/calendar/end-date", data={"end_date": end.isoformat()})


class TestTimetablePage:
    def test_inferred_slots_are_offered_with_confidence(self, client):
        html = client.get("/timetable").get_data(as_text=True)
        assert "Your week" in html
        assert "4 weeks" in html          # confident patterns
        assert "Seen once" in html        # the single outlier

    def test_confirming_saves_a_version(self, client, app):
        resp, tokens = confirm_timetable(client)
        assert resp.status_code == 302
        with app.app_context():
            assert db.session.query(TimetableSlot).count() == len(tokens) == 32

    def test_reconfirming_creates_a_new_version_not_an_edit(self, client, app):
        confirm_timetable(client)
        confirm_timetable(client)
        with app.app_context():
            from app.models import TimetableVersion

            assert db.session.query(TimetableVersion).count() == 2

    def test_foreign_subject_ids_are_ignored(self, client, app):
        resp = client.post("/timetable", data={"slot": ["9999|0|09:00:00|10:00:00"]})
        with app.app_context():
            assert db.session.query(TimetableSlot).count() == 0


class TestCalendarPage:
    def test_needs_a_future_end_date(self, client):
        resp = client.post("/calendar/end-date", data={"end_date": "2026-01-01"},
                           follow_redirects=True)
        assert "future" in resp.get_data(as_text=True)

    def test_tapping_a_day_cycles_it(self, client, app):
        target = (TODAY + timedelta(days=3)).isoformat()
        resp = client.post("/api/calendar/day", json={"date": target, "kind": "holiday"})
        assert resp.status_code == 200
        with app.app_context():
            assert db.session.query(Holiday).one().kind == "holiday"

        resp = client.post("/api/calendar/day",
                           json={"date": target, "kind": "swap", "swap_weekday": 1})
        with app.app_context():
            row = db.session.query(Holiday).one()
            assert (row.kind, row.swap_weekday) == ("swap", 1)

        client.post("/api/calendar/day", json={"date": target, "kind": None})
        with app.app_context():
            assert db.session.query(Holiday).count() == 0

    def test_the_past_is_read_only(self, client):
        resp = client.post("/api/calendar/day",
                           json={"date": "2026-08-01", "kind": "holiday"})
        assert resp.status_code == 400

    def test_swap_requires_a_weekday(self, client):
        target = (TODAY + timedelta(days=3)).isoformat()
        resp = client.post("/api/calendar/day", json={"date": target, "kind": "swap"})
        assert resp.status_code == 400


class TestAdvancedDashboard:
    def test_before_setup_the_dashboard_nudges(self, client):
        html = client.get("/").get_data(as_text=True)
        assert "Confirm timetable" in html

    def test_after_setup_the_hero_verdict_appears(self, client):
        confirm_timetable(client)
        set_end(client)
        html = client.get("/").get_data(as_text=True)

        assert "hero-verdict" in html
        assert "Bunks left this semester" in html
        assert "day-strip" in html

    def test_timetable_and_calendar_stay_reachable_after_setup(self, client):
        """Both were only linked from 'not set up yet' branches, so once advanced
        mode was configured they became unreachable — even though holidays get
        announced mid-semester and timetables get reshuffled."""
        confirm_timetable(client)
        set_end(client)

        for page in ("/", "/plan"):
            html = client.get(page).get_data(as_text=True)
            assert 'href="/calendar"' in html, f"no way to reach the calendar from {page}"
            assert 'href="/timetable"' in html, f"no way to reach the timetable from {page}"

    def test_held_since_last_report_is_surfaced(self, client):
        """Report covers to 12.08; today is 16.08 — Thu 13.08 and Fri 14.08
        held lectures nobody has reported. They must be shown, not ignored."""
        confirm_timetable(client)
        set_end(client)
        html = client.get("/").get_data(as_text=True)
        assert "Held since your last report" in html


class TestWalletApi:
    def test_wallet_shape_and_worst_case_honesty(self, client):
        confirm_timetable(client)
        set_end(client)
        body = client.post("/api/simulate", json={"absences": []}).get_json()

        assert body["overall_limit"] == 75
        assert body["overall_remaining"] > 100        # ~15 weeks of Mon-Fri left
        assert len(body["days"]) > 0
        ds = next(s for s in body["subjects"] if s["code"] == "DS")
        assert ds["unreported"] >= 1                  # Thu 13.08 held a DS slot
        assert ds["projected_total"] > ds["remaining"]

    def test_hypothetical_absences_change_nothing_stored(self, client, app):
        confirm_timetable(client)
        set_end(client)
        target = (TODAY + timedelta(days=1)).isoformat()

        before = client.post("/api/simulate", json={"absences": []}).get_json()
        after = client.post("/api/simulate",
                            json={"absences": [{"date": target}]}).get_json()

        assert after["overall_planned"] > before["overall_planned"]
        with app.app_context():
            assert db.session.query(PlannedAbsence).count() == 0

    def test_committing_an_absence_stores_and_recomputes(self, client, app):
        confirm_timetable(client)
        set_end(client)
        target = (TODAY + timedelta(days=1)).isoformat()   # Mon 17.08: 7 lectures

        body = client.post("/api/absences", json={"date": target}).get_json()
        assert body["overall_planned"] == 7
        with app.app_context():
            assert db.session.query(PlannedAbsence).count() == 1

    def test_removing_an_absence_restores_the_budget(self, client, app):
        confirm_timetable(client)
        set_end(client)
        target = (TODAY + timedelta(days=1)).isoformat()
        client.post("/api/absences", json={"date": target})
        with app.app_context():
            absence_id = db.session.query(PlannedAbsence).one().id

        body = client.delete(f"/api/absences/{absence_id}").get_json()
        assert body["overall_planned"] == 0

    def test_a_holiday_removes_lectures_from_projection(self, client):
        confirm_timetable(client)
        set_end(client)
        before = client.post("/api/simulate", json={"absences": []}).get_json()

        # Monday 24.08 carries 7 lectures; declare it a holiday.
        client.post("/api/calendar/day",
                    json={"date": "2026-08-24", "kind": "holiday"})
        after = client.post("/api/simulate", json={"absences": []}).get_json()

        assert after["overall_remaining"] == before["overall_remaining"] - 7

    def test_days_reflect_verdicts(self, client):
        confirm_timetable(client)
        set_end(client)
        body = client.post("/api/simulate", json={"absences": []}).get_json()
        verdicts = {d["verdict"] for d in body["days"]}
        assert verdicts <= {"skip", "partial", "go", "off"}
        # Weekends carry no classes in this timetable.
        sunday = next(d for d in body["days"] if date.fromisoformat(d["date"]).weekday() == 6)
        assert sunday["verdict"] == "off"


class TestPlanPage:
    def test_not_ready_shows_the_next_step(self, client):
        html = client.get("/plan").get_data(as_text=True)
        assert "Confirm your timetable" in html

    def test_ready_shows_wallet_and_strip(self, client):
        confirm_timetable(client)
        set_end(client)
        html = client.get("/plan").get_data(as_text=True)
        assert "Bunks left" in html
        assert "Committed absences" in html
        assert "Where each subject lands" in html
