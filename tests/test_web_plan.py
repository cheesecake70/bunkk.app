"""Phase 2 web layer: timetable confirmation, calendar, wallet, day strip.

The fixture drives the whole advanced-mode setup the way a user would: upload
the real PDF, accept the inferred timetable, set the semester end date.
"""
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from conftest import google_sign_in

import app.planning as planning_module
from app import create_app, db
from app.models import Holiday, PlannedAbsence, Subject, TimetableSlot, User

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
    import app.routes as routes_module

    # Freeze "today" in every module a request can ask it from. All three need
    # it: the API's "that day has already happened" guards and the routes'
    # week navigator each call date.today() themselves, so patching only
    # planning left these tests passing purely because the wall clock happened
    # to sit near TODAY.
    class FrozenDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(planning_module, "date", FrozenDate)
    monkeypatch.setattr(api_module, "date", FrozenDate)
    monkeypatch.setattr(routes_module, "date", FrozenDate)

    client = app.test_client()
    google_sign_in(client, "m@example.com", "muser")
    client.post(
        "/api/reports",
        data={"report": (GOLDEN.open("rb"), "report.pdf")},
        content_type="multipart/form-data",
    )
    return client


def grid_blocks(client):
    """The blocks the timetable page is currently showing, as parallel arrays.

    Reads them back out of the rendered grid the way the page's own JS does, so
    the tests exercise the real serialisation rather than a parallel guess at it.
    """
    page = client.get("/timetable").get_data(as_text=True)
    import re

    blocks = re.findall(
        r'data-kind="([^"]*)"\s+data-weekday="([^"]*)"\s+'
        r'data-start="([^"]*)"\s+data-end="([^"]*)"\s+data-subject="([^"]*)"',
        page,
    )
    return {
        "kind": [b[0] for b in blocks],
        "weekday": [b[1] for b in blocks],
        "start": [b[2] for b in blocks],
        "end": [b[3] for b in blocks],
        "subject_id": [b[4] for b in blocks],
        "label": ["" for _ in blocks],
    }


def confirm_timetable(client):
    """Save the grid the page offers — the inferred draft, untouched."""
    data = grid_blocks(client)
    assert data["kind"], "expected an inferred grid on the timetable page"
    return client.post("/timetable", data=data, follow_redirects=False), data


def set_end(client, end=SEMESTER_END):
    return client.post("/calendar/end-date", data={"end_date": end.isoformat()})


class TestTimetablePage:
    def test_the_grid_is_drafted_from_the_reports(self, client):
        """Inference is the starting point, not the final word — the page says so
        and every block it drew is editable."""
        html = client.get("/timetable").get_data(as_text=True)
        assert "Your week" in html
        assert "Drafted from your reports" in html
        assert 'class="tt__block' in html

    def test_clashing_blocks_are_flagged(self, client):
        """The golden grid runs two subjects at Thursday 13:00. Unlabelled, that
        reads as the page having drawn the same hour twice."""
        html = client.get("/timetable").get_data(as_text=True)
        assert "is-overlap" in html
        assert "shares this slot" in html

    def test_the_first_save_points_at_the_missing_end_date(self, client):
        """A grid alone projects nothing, and the page looked finished."""
        resp, _ = confirm_timetable(client)
        html = client.get("/timetable").get_data(as_text=True)
        assert "set your semester end date" in html

    def test_a_later_save_does_not_nag(self, client):
        confirm_timetable(client)
        set_end(client)
        confirm_timetable(client)
        html = client.get("/timetable").get_data(as_text=True)
        assert "set your semester end date" not in html

    def test_saving_the_grid_stores_a_version(self, client, app):
        resp, data = confirm_timetable(client)
        assert resp.status_code == 302
        with app.app_context():
            assert db.session.query(TimetableSlot).count() == len(data["kind"])
            assert db.session.query(TimetableSlot).filter_by(kind="class").count() == 32

    def test_the_draft_fills_free_periods_in_as_breaks(self, client, app):
        """A gap between two inferred classes is a free period whether or not
        anyone typed one, and saying so is what makes "leave before lunch" worth
        more than "leave at noon and sit around"."""
        _, data = confirm_timetable(client)
        assert "break" in data["kind"], "the drafted grid drew no breaks"

        with app.app_context():
            breaks = db.session.query(TimetableSlot).filter_by(kind="break").all()
            assert breaks
            for row in breaks:
                assert row.subject_id is None
                gap = ((row.end_time.hour * 60 + row.end_time.minute)
                       - (row.start_time.hour * 60 + row.start_time.minute))
                assert 15 <= gap <= 180

    def test_a_break_is_saved_but_never_counted(self, client, app):
        """Breaks make the grid honest without moving a single number."""
        confirm_timetable(client)
        set_end(client)
        before = client.post("/api/simulate", json={"absences": []}).get_json()

        data = grid_blocks(client)
        data["kind"].append("break")
        data["weekday"].append("0")
        data["start"].append("12:00:00")
        data["end"].append("13:00:00")
        data["subject_id"].append("")
        data["label"].append("Lunch")
        client.post("/timetable", data=data)

        with app.app_context():
            stored = db.session.query(TimetableSlot).filter_by(kind="break").all()
            lunch = [b for b in stored if b.label == "Lunch" and b.weekday == 0]
            assert lunch, "the break the user drew was not stored"
            assert all(b.subject_id is None for b in stored)

        after = client.post("/api/simulate", json={"absences": []}).get_json()
        assert after["overall_remaining"] == before["overall_remaining"]
        assert after["overall_budget"] == before["overall_budget"]

    def test_saving_twice_in_a_day_rewrites_one_version(self, client, app):
        """The editor saves on every Done now, so a session of tidying the grid
        would otherwise leave a version per dialog close and bury the history
        it exists to keep. Same day, same source: rewrite in place."""
        confirm_timetable(client)
        confirm_timetable(client)
        with app.app_context():
            from app.models import TimetableVersion

            versions = db.session.query(TimetableVersion).all()
            assert len(versions) == 1
            # And the grid it holds is the one just posted, not a merge of both.
            assert db.session.query(TimetableSlot).filter_by(
                version_id=versions[0].id
            ).count() == db.session.query(TimetableSlot).count()

    def test_a_reshuffle_on_a_later_day_starts_a_new_version(self, client, app):
        """Versions are effective-dated so past projections stay explicable —
        collapsing same-day saves must not collapse across days."""
        confirm_timetable(client)
        with app.app_context():
            from app.models import TimetableVersion
            from app import planning

            user = db.session.query(User).one()
            planning.save_timetable(
                user,
                planning.entries_from([{
                    "kind": "class", "weekday": "0",
                    "start": "09:00", "end": "10:00",
                    "subject_id": str(db.session.query(Subject).first().id),
                }]),
                source="manual",
                valid_from=date.today() + timedelta(days=7),
            )
            assert db.session.query(TimetableVersion).count() == 2

    def test_foreign_subject_ids_are_ignored(self, client, app):
        """Subject ids arrive from a form, so they are never trusted."""
        client.post("/timetable", data={
            "kind": ["class"], "weekday": ["0"],
            "start": ["09:00:00"], "end": ["10:00:00"],
            "subject_id": ["9999"], "label": [""],
        })
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
            assert db.session.query(Holiday).count() == 1

        client.post("/api/calendar/day", json={"date": target, "kind": None})
        with app.app_context():
            assert db.session.query(Holiday).count() == 0

    def test_the_past_is_read_only(self, client):
        resp = client.post("/api/calendar/day",
                           json={"date": "2026-08-01", "kind": "holiday"})
        assert resp.status_code == 400


class TestHolidayRanges:
    """A mid-sem break is a week, not seven separate decisions."""

    def test_a_range_marks_every_day_in_it(self, client, app):
        confirm_timetable(client)
        set_end(client)
        resp = client.post("/api/calendar/range", json={
            "from": "2026-08-24", "to": "2026-08-28", "name": "Reading week",
        })
        assert resp.status_code == 200
        assert len(resp.get_json()["dates"]) == 5
        with app.app_context():
            assert db.session.query(Holiday).count() == 5

    def test_clearing_a_range_puts_the_days_back(self, client, app):
        confirm_timetable(client)
        set_end(client)
        client.post("/api/calendar/range", json={"from": "2026-08-24", "to": "2026-08-28"})
        client.post("/api/calendar/range",
                    json={"from": "2026-08-24", "to": "2026-08-28", "kind": None})
        with app.app_context():
            assert db.session.query(Holiday).count() == 0

    def test_the_lectures_in_it_stop_counting(self, client):
        confirm_timetable(client)
        set_end(client)
        before = client.post("/api/simulate", json={"absences": []}).get_json()
        client.post("/api/calendar/range", json={"from": "2026-08-24", "to": "2026-08-28"})
        after = client.post("/api/simulate", json={"absences": []}).get_json()
        assert after["overall_remaining"] < before["overall_remaining"]

    def test_a_backwards_range_is_refused(self, client):
        confirm_timetable(client)
        set_end(client)
        resp = client.post("/api/calendar/range",
                           json={"from": "2026-08-28", "to": "2026-08-24"})
        assert resp.status_code == 400

    def test_a_range_in_the_past_is_refused(self, client, app):
        confirm_timetable(client)
        set_end(client)
        resp = client.post("/api/calendar/range",
                           json={"from": "2026-08-01", "to": "2026-08-05"})
        assert resp.status_code == 400
        with app.app_context():
            assert db.session.query(Holiday).count() == 0

    def test_an_implausibly_long_range_is_refused(self, client):
        """More likely a mistyped year than a holiday."""
        confirm_timetable(client)
        set_end(client)
        resp = client.post("/api/calendar/range",
                           json={"from": "2026-08-24", "to": "2027-08-24"})
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
        assert "week-nav" in html          # this week, walkable with the arrows

    def test_a_fully_planned_day_reads_planned_on_the_hero(self, client):
        """The walkthrough's headline bug: "Skip whole day" left a green SKIP
        hero over a day the student had just spent."""
        confirm_timetable(client)
        set_end(client)
        client.post("/api/absences", json={"date": "2026-08-17"})

        html = client.get("/?date=2026-08-17").get_data(as_text=True)
        assert "hero-verdict--planned" in html
        assert "PLANNED" in html
        assert "whole day" in html

    def test_a_no_class_day_points_at_the_next_one(self, client):
        """A Saturday used to be a dead end: "No classes", and nothing else."""
        confirm_timetable(client)
        set_end(client)
        # TODAY is a Sunday; the timetable is Monday to Friday.
        html = client.get("/").get_data(as_text=True)
        assert "Next classes" in html
        assert "date=2026-08-17" in html

    def test_a_holiday_is_named_on_the_hero(self, client):
        confirm_timetable(client)
        set_end(client)
        client.post("/api/calendar/day",
                    json={"date": "2026-08-17", "kind": "holiday", "name": "Onam"})

        html = client.get("/?date=2026-08-17").get_data(as_text=True)
        assert "ONAM" in html
        assert "No classes count today." in html

    def test_before_setup_today_still_shows_the_ledger(self, client):
        """Without a timetable there are no verdicts — which used to leave this
        page with a setup card and a single link on it."""
        html = client.get("/").get_data(as_text=True)
        assert "Overall attendance" in html
        assert "Attended" in html

    def test_today_nudges_when_the_report_is_stale(self, client):
        """The most common reason a number here looks wrong."""
        html = client.get("/").get_data(as_text=True)
        assert "since your last report" in html
        assert "Upload" in html

    def test_today_lists_the_days_classes(self, client):
        """Today's whole job: what have I got on, and can I skip it."""
        confirm_timetable(client)
        set_end(client)
        # 2026-08-16 is a Sunday, so the window runs 16-22 Aug; Monday is in it.
        html = client.get("/?date=2026-08-17").get_data(as_text=True)
        assert "js-skip-lecture" in html

    def test_a_planned_lecture_renders_as_already_skipping(self, client, app):
        """The button used to flip to "Skipping" and then reload into a page
        that always said "Skip this" — the state has to survive the round trip."""
        confirm_timetable(client)
        set_end(client)
        lectures = client.get("/api/day/2026-08-17").get_json()["lectures"]
        first = lectures[0]

        before = client.get("/?date=2026-08-17").get_data(as_text=True)
        assert "Skipping" not in before

        client.post("/api/absences", json={
            "date": "2026-08-17",
            "subject_id": first["subject_id"],
            "start": first["start"],
        })

        after = client.get("/?date=2026-08-17").get_data(as_text=True)
        assert "Skipping" in after
        assert "data-absence-id" in after      # so a second click can undo it

    def test_a_whole_day_plan_locks_the_per_lecture_buttons(self, client):
        """One absence covers every class that day; it can only be undone whole."""
        confirm_timetable(client)
        set_end(client)
        client.post("/api/absences", json={"date": "2026-08-17"})

        html = client.get("/?date=2026-08-17").get_data(as_text=True)
        assert "The whole day is planned off" in html

    def test_today_offers_one_button_for_the_whole_day(self, client):
        """Skipping everything shouldn't cost one tap per class."""
        confirm_timetable(client)
        set_end(client)
        html = client.get("/?date=2026-08-17").get_data(as_text=True)
        assert "js-skip-day" in html
        assert "Skip whole day" in html

    def test_the_whole_day_button_reflects_an_existing_plan(self, client):
        """Same round-trip rule as the per-lecture buttons: the label and the id
        come from the server, so a reload can still undo it."""
        confirm_timetable(client)
        set_end(client)
        posted = client.post("/api/absences", json={"date": "2026-08-17"})
        absence_id = posted.get_json()["absence_id"]

        html = client.get("/?date=2026-08-17").get_data(as_text=True)
        assert "Skipping all day" in html
        assert 'class="btn btn--sm btn--danger js-skip-day"' in html
        assert 'data-absence-id="%d"' % absence_id in html

    def test_a_lecture_under_a_whole_day_plan_keeps_its_own_state(self, client):
        """Lifting the day plan has to put each class back the way it was, so
        the per-lecture absence is rendered even while the day plan hides it."""
        confirm_timetable(client)
        set_end(client)
        first = client.get("/api/day/2026-08-17").get_json()["lectures"][0]
        own = client.post("/api/absences", json={
            "date": "2026-08-17",
            "subject_id": first["subject_id"],
            "start": first["start"],
        }).get_json()["absence_id"]
        client.post("/api/absences", json={"date": "2026-08-17"})

        html = client.get("/?date=2026-08-17").get_data(as_text=True)
        assert 'data-own-absence="%d"' % own in html
        assert 'data-own-absence=""' in html          # the rest own nothing

    def test_today_links_each_class_to_its_subject(self, client):
        confirm_timetable(client)
        set_end(client)
        html = client.get("/?date=2026-08-17").get_data(as_text=True)
        assert "lecture__subject" in html
        assert "/subjects/" in html

    def test_the_strip_walks_past_its_seventh_day(self, client):
        """It used to be pinned to today, so day seven was a wall: the arrow
        greyed out with nothing to say about why."""
        confirm_timetable(client)
        set_end(client)

        edge = TODAY + timedelta(days=6)
        html = client.get(f"/?date={edge.isoformat()}").get_data(as_text=True)
        # The next day is reachable rather than the arrow being dead.
        assert f'date={(edge + timedelta(days=1)).isoformat()}' in html

        beyond = TODAY + timedelta(days=30)
        html = client.get(f"/?date={beyond.isoformat()}").get_data(as_text=True)
        assert beyond.strftime("%d %b") in html

    def test_the_window_slides_but_never_shows_the_past(self, client):
        confirm_timetable(client)
        set_end(client)
        focus = TODAY + timedelta(days=20)
        html = client.get(f"/?date={focus.isoformat()}").get_data(as_text=True)

        # Seven days ending on the focused one, none of them behind today.
        for offset in range(7):
            day = focus - timedelta(days=offset)
            assert f'date={day.isoformat()}' in html
        assert f'date={(TODAY - timedelta(days=1)).isoformat()}' not in html

    def test_you_cannot_walk_past_the_end_of_term(self, client):
        confirm_timetable(client)
        set_end(client)
        html = client.get(f"/?date={SEMESTER_END.isoformat()}").get_data(as_text=True)
        assert f'date={(SEMESTER_END + timedelta(days=1)).isoformat()}' not in html
        assert "end of your semester" in html

    def test_a_date_outside_the_term_lands_on_the_nearest_real_day(self, client):
        """Clamped rather than discarded — silently bouncing back to today reads
        as the app having ignored you."""
        confirm_timetable(client)
        set_end(client)

        past = client.get("/?date=2026-01-01").get_data(as_text=True)
        assert TODAY.strftime("%d %b %Y") in past

        future = client.get("/?date=2027-06-01").get_data(as_text=True)
        assert SEMESTER_END.strftime("%d %b %Y") in future

    def test_without_an_end_date_the_strip_stays_a_week(self, client):
        """Nothing to project through, so nothing to walk through."""
        confirm_timetable(client)
        edge = TODAY + timedelta(days=6)
        html = client.get(f"/?date={edge.isoformat()}").get_data(as_text=True)
        assert f'date={(edge + timedelta(days=1)).isoformat()}' not in html

    def test_plan_carries_the_semester_wide_numbers(self, client):
        confirm_timetable(client)
        set_end(client)
        html = client.get("/plan").get_data(as_text=True)
        assert "Attended" in html
        assert "Subjects" in html

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
        assert verdicts <= {"skip", "partial", "go", "off", "planned"}
        # Weekends carry no classes in this timetable.
        sunday = next(d for d in body["days"] if date.fromisoformat(d["date"]).weekday() == 6)
        assert sunday["verdict"] == "off"

    def test_a_committed_day_stops_asking_you_to_decide_it(self, client):
        """The budget already paid for those lectures. Re-charging it read back
        as "part skip" on a day that was settled — and calling it "skip" told
        the student a decision they had already taken was safe advice."""
        confirm_timetable(client)
        set_end(client)
        client.post("/api/absences", json={"date": "2026-08-17"})

        body = client.post("/api/simulate", json={"absences": []}).get_json()
        day = next(d for d in body["days"] if d["date"] == "2026-08-17")
        assert day["verdict"] == "planned"
        assert day["whole_day"] is True
        assert day["planned_count"] == len(day["lectures"])
        assert "Skipping the whole day" in day["reason"]

    def test_a_light_simulation_skips_the_strip(self, client):
        """The pre-commit check only asks "would this break anything?", so it
        never pays for a projection of the whole horizon."""
        confirm_timetable(client)
        set_end(client)
        body = client.post("/api/simulate",
                           json={"absences": [], "light": True}).get_json()
        assert "days" not in body
        assert "is_safe" in body and "breaks" in body

    def test_an_absence_answers_with_that_days_fresh_plan(self, client):
        """Today repaints its hero from this instead of reloading the page."""
        confirm_timetable(client)
        set_end(client)
        body = client.post("/api/absences",
                           json={"date": "2026-08-17"}).get_json()
        assert body["day"]["date"] == "2026-08-17"
        assert body["day"]["verdict"] == "planned"

        removed = client.delete(f"/api/absences/{body['absence_id']}").get_json()
        assert removed["day"]["date"] == "2026-08-17"
        assert removed["day"]["verdict"] != "planned"

    def test_committing_an_unsafe_absence_is_reported_not_refused(self, client):
        """The plan is the student's to make; the app's job is to say what it
        costs. The UI leans on `breaks` to warn before and after."""
        confirm_timetable(client)
        set_end(client)
        client.post("/settings", data={"subject_limit": "95", "overall_limit": "95"})

        body = client.post("/api/absences", json={"date": "2026-08-17"}).get_json()
        assert body["is_safe"] is False
        assert body["breaks"]
        with client.application.app_context():
            assert db.session.query(PlannedAbsence).count() >= 1


class TestPlanPage:
    def test_not_ready_still_shows_attendance_and_the_next_step(self, client):
        """A missing timetable costs you the planning half, not your ledger."""
        html = client.get("/plan").get_data(as_text=True)
        assert "Confirm timetable" in html
        assert "Attended" in html

    def test_ready_shows_the_month_grid_and_the_plan_controls(self, client):
        """The horizon is months long, so it is drawn as months. As a single
        row of chips it was seventy near-identical cells to scroll past."""
        confirm_timetable(client)
        set_end(client)
        html = client.get("/plan").get_data(as_text=True)
        assert "plan-month" in html
        assert "day-strip" not in html
        assert "Committed absences" in html
        assert "What if I skip one subject?" in html

    def test_weekends_get_no_column_when_nothing_runs_on_them(self, client):
        """The golden timetable is Monday to Friday."""
        confirm_timetable(client)
        set_end(client)
        html = client.get("/plan").get_data(as_text=True)
        assert "cal--5" in html
        assert "cal__dow" in html
        assert ">Sun<" not in html

    def test_the_strip_opens_the_day_sheet_rather_than_toggling(self, client):
        """A day is rarely all-or-nothing, so the strip hands off to the sheet
        instead of committing the whole day on one click."""
        confirm_timetable(client)
        set_end(client)
        html = client.get("/plan").get_data(as_text=True)
        assert 'id="day-sheet"' in html          # the sheet is on the page
        assert "js-day" in html                  # days still open it
        assert "data-absence-id" not in html     # but no longer toggle by themselves

    def test_the_strip_counts_what_is_planned_on_each_day(self, client, app):
        confirm_timetable(client)
        set_end(client)
        lectures = client.get("/api/day/2026-08-17").get_json()["lectures"]
        client.post("/api/absences", json={
            "date": "2026-08-17",
            "subject_id": lectures[0]["subject_id"],
            "start": lectures[0]["start"],
        })

        html = client.get("/plan").get_data(as_text=True)
        assert "cal__count" in html

    def test_a_whole_day_and_a_single_class_are_not_counted_twice(self, client, app):
        """Both cover the same lecture; the day can't be missed more than once."""
        confirm_timetable(client)
        set_end(client)
        lectures = client.get("/api/day/2026-08-17").get_json()["lectures"]
        client.post("/api/absences", json={"date": "2026-08-17"})
        client.post("/api/absences", json={
            "date": "2026-08-17",
            "subject_id": lectures[0]["subject_id"],
            "start": lectures[0]["start"],
        })

        with app.app_context():
            user = db.session.get(User, 1)
            rows = db.session.query(PlannedAbsence).all()
            assert len(rows) == 2                  # both really were stored
            counts = planning_module.planned_count_by_date(user, rows)
        assert counts[date(2026, 8, 17)] == len(lectures)

    def test_the_budget_you_can_actually_spend_is_on_the_page(self, client):
        """`wallet.overall_budget` was computed on every load and shown nowhere —
        the single number a student most wants."""
        confirm_timetable(client)
        set_end(client)
        html = client.get("/plan").get_data(as_text=True)
        assert "Can still miss" in html

    def test_now_and_projected_are_labelled(self, client):
        """A red 25% beside a green SAFE is two different questions, and the
        page used to answer both without saying which was which."""
        confirm_timetable(client)
        set_end(client)
        html = client.get("/plan").get_data(as_text=True)
        assert "Now · worst case" in html or "Now" in html
        assert "Projected to" in html

    def test_zero_headroom_reads_as_no_slack_rather_than_tight(self, client):
        """"DBMS Lab · 100% · TIGHT" reads as a warning about the percentage."""
        confirm_timetable(client)
        set_end(client)
        client.post("/settings", data={"subject_limit": "95", "overall_limit": "95"})
        html = client.get("/plan").get_data(as_text=True)
        assert "No slack yet" in html

    def test_the_table_pairs_ledger_and_projection_in_one_row(self, client):
        """The whole point of the merge: one row answers both questions."""
        confirm_timetable(client)
        set_end(client)
        html = client.get("/plan").get_data(as_text=True)
        assert "Can miss" in html          # from the wallet
        assert "Pending" in html           # from the ledger


class TestSkipLadderApi:
    def test_it_answers_what_skipping_n_of_one_subject_costs(self, client, app):
        confirm_timetable(client)
        set_end(client)
        with app.app_context():
            subject_id = db.session.query(Subject).filter_by(code="DS").one().id

        body = client.get(f"/api/subjects/{subject_id}/skip-ladder").get_json()
        assert body["subject"]["code"] == "DS"
        assert [r["n"] for r in body["ladder"]] == list(
            range(1, len(body["ladder"]) + 1)
        )
        # Percentages fall as you skip more; anything else is a sign error.
        pcts = [r["subject_pct"] for r in body["ladder"]]
        assert pcts == sorted(pcts, reverse=True)

    def test_the_dates_it_offers_are_real_lectures_of_that_subject(self, client, app):
        confirm_timetable(client)
        set_end(client)
        with app.app_context():
            subject_id = db.session.query(Subject).filter_by(code="DS").one().id

        body = client.get(f"/api/subjects/{subject_id}/skip-ladder").get_json()
        for slot in body["next_dates"]:
            resp = client.post("/api/absences", json={
                "date": slot["date"], "subject_id": subject_id, "start": slot["start"],
            })
            assert resp.status_code == 200, resp.get_json()

    def test_another_users_subject_is_not_found(self, client, app):
        confirm_timetable(client)
        set_end(client)
        assert client.get("/api/subjects/9999/skip-ladder").status_code == 404


class TestSubjectPageLadder:
    """The ladder is asked from /plan and from a subject's own page.

    The subject page renders only the toggle — the rungs arrive from the same
    endpoint /plan uses, so there is nothing here to assert twice.
    """

    def _subject_id(self, app):
        with app.app_context():
            return db.session.query(Subject).filter_by(code="DS").one().id

    def test_the_page_offers_the_ladder_once_planning_is_set_up(self, client, app):
        confirm_timetable(client)
        set_end(client)
        html = client.get(f"/subjects/{self._subject_id(app)}").get_data(as_text=True)
        assert "What if I skip more?" in html
        assert 'id="ladder-toggle"' in html
        assert "js/ladder.js" in html

    def test_it_is_hidden_until_there_is_a_timetable_to_project(self, client, app):
        """No timetable means no future lectures, so the answer would be empty."""
        html = client.get(f"/subjects/{self._subject_id(app)}").get_data(as_text=True)
        assert "What if I skip more?" not in html


class TestEmptyDayAbsences:
    """A day with nothing on it can't be missed.

    Committing one used to succeed and then sit in the committed list saying
    "30 Aug — whole day", spending nothing and meaning nothing.
    """

    def test_a_weekend_whole_day_is_refused(self, client):
        confirm_timetable(client)
        set_end(client)
        sunday = TODAY + timedelta(days=(6 - TODAY.weekday()) % 7 or 7)
        assert sunday.weekday() == 6

        resp = client.post("/api/absences", json={"date": sunday.isoformat()})
        assert resp.status_code == 422
        assert "No classes" in resp.get_json()["error"]

    def test_a_holiday_whole_day_is_refused(self, client, app):
        confirm_timetable(client)
        set_end(client)
        monday = TODAY + timedelta(days=(0 - TODAY.weekday()) % 7 or 7)
        client.post("/api/calendar/day",
                    json={"date": monday.isoformat(), "kind": "holiday"})

        resp = client.post("/api/absences", json={"date": monday.isoformat()})
        assert resp.status_code == 422
        with app.app_context():
            assert db.session.query(PlannedAbsence).count() == 0


class TestPerLectureAbsences:
    def test_two_lectures_of_one_subject_on_one_day_are_separable(self, client, app):
        """A real timetable runs some subjects twice in a day. Keying an absence
        on (date, subject) made the second one unplannable."""
        confirm_timetable(client)
        set_end(client)

        # Find a subject the grid genuinely runs twice on the same weekday,
        # rather than assuming which one it is.
        with app.app_context():
            slots = db.session.query(TimetableSlot).all()
            seen: dict[tuple, list] = {}
            for slot in slots:
                seen.setdefault((slot.subject_id, slot.weekday), []).append(slot.start_time)
            doubled = next(
                ((sid, wd, times) for (sid, wd), times in seen.items() if len(times) > 1),
                None,
            )
        assert doubled, "the golden timetable should double-book at least one subject"
        subject_id, weekday, times = doubled

        day = TODAY + timedelta(days=(weekday - TODAY.weekday()) % 7 or 7)
        responses = [
            client.post("/api/absences", json={
                "date": day.isoformat(), "subject_id": subject_id,
                "start": start.isoformat(),
            })
            for start in sorted(times)[:2]
        ]

        assert [r.status_code for r in responses] == [200, 200]
        with app.app_context():
            assert db.session.query(PlannedAbsence).count() == 2

    def test_an_absence_against_a_class_that_does_not_run_is_refused(self, client, app):
        """Previously this stored a row that quietly counted nothing."""
        confirm_timetable(client)
        set_end(client)
        with app.app_context():
            subject_id = db.session.query(Subject).filter_by(code="DBMS Lab").one().id

        sunday = (TODAY + timedelta(days=7)).isoformat()
        resp = client.post("/api/absences",
                           json={"date": sunday, "subject_id": subject_id})
        assert resp.status_code == 422
        with app.app_context():
            assert db.session.query(PlannedAbsence).count() == 0

    def test_an_absence_past_the_semester_end_is_refused(self, client):
        """There was no upper bound at all — you could plan to miss 2099."""
        confirm_timetable(client)
        set_end(client)
        resp = client.post("/api/absences", json={"date": "2099-01-01"})
        assert resp.status_code == 400


class TestPartialDays:
    """"Leave after 12:00" is one decision, and the break it clears is worth an
    hour of the day even though it is worth nothing to the budget."""

    def _tighten(self, client):
        # Loose limits make every day outright skippable; a partial day is the
        # interesting case.
        client.post("/settings", data={"subject_limit": "85", "overall_limit": "88"})

    def test_the_sheet_draws_the_breaks_and_offers_the_cut(self, client):
        confirm_timetable(client)
        set_end(client)
        self._tighten(client)

        body = client.get(f"/api/day/{(TODAY + timedelta(days=1)).isoformat()}").get_json()
        assert body["breaks"], "Monday's free period should be in the sheet"
        assert all(b["label"] for b in body["breaks"])

        partial = body["partial"]
        assert partial, "a tightened Monday should be a part-skip"
        assert partial["leave_after"] or partial["arrive_at"]
        assert partial["skippable"]
        assert partial["freed_minutes"] > 0

    def test_the_cut_counts_the_break_it_clears(self, client):
        confirm_timetable(client)
        set_end(client)
        self._tighten(client)

        monday = (TODAY + timedelta(days=1)).isoformat()
        partial = client.get(f"/api/day/{monday}").get_json()["partial"]
        assert partial["covers_break"], "the cut should name the break it clears"
        assert partial["covers_break"] in partial["reason"]
        # More time off than the lectures alone account for — that difference
        # is the free hour, which is the whole reason to cut here.
        assert partial["freed_minutes"] > 60 * len(partial["skippable"])

    def test_taking_it_commits_every_lecture_in_one_request(self, client, app):
        """Posting them one by one raced four wallet recomputations against
        each other and half-applied the plan."""
        confirm_timetable(client)
        set_end(client)
        self._tighten(client)

        monday = (TODAY + timedelta(days=1)).isoformat()
        partial = client.get(f"/api/day/{monday}").get_json()["partial"]

        resp = client.post("/api/absences/batch",
                           json={"date": monday, "lectures": partial["skippable"]})
        assert resp.status_code == 200
        ids = resp.get_json()["absence_ids"]
        assert len(ids) == len(partial["skippable"])

        with app.app_context():
            assert db.session.query(PlannedAbsence).count() == len(partial["skippable"])

        after = client.get(f"/api/day/{monday}").get_json()
        planned = [l for l in after["lectures"] if l["absence_id"]]
        assert len(planned) == len(partial["skippable"])

    def test_a_batch_is_all_or_nothing(self, client, app):
        """A half-applied "leave after 12:00" is a plan nobody made."""
        confirm_timetable(client)
        set_end(client)
        monday = (TODAY + timedelta(days=1)).isoformat()
        real = client.get(f"/api/day/{monday}").get_json()["lectures"][0]

        resp = client.post("/api/absences/batch", json={"date": monday, "lectures": [
            {"subject_id": real["subject_id"], "start": real["start"]},
            {"subject_id": real["subject_id"], "start": "23:00:00"},   # never runs
        ]})
        assert resp.status_code == 422
        with app.app_context():
            assert db.session.query(PlannedAbsence).count() == 0

    def test_a_batch_for_someone_elses_subject_is_refused(self, app, client):
        confirm_timetable(client)
        set_end(client)
        monday = (TODAY + timedelta(days=1)).isoformat()
        real = client.get(f"/api/day/{monday}").get_json()["lectures"][0]

        other = app.test_client()
        google_sign_in(other, "other@example.com", "other")
        resp = other.post("/api/absences/batch", json={
            "date": monday, "lectures": [real]})
        assert resp.status_code == 404


class TestDaySheet:
    def test_it_lists_a_days_lectures_with_what_is_planned(self, client, app):
        confirm_timetable(client)
        set_end(client)
        monday = (TODAY + timedelta(days=1)).isoformat()

        body = client.get(f"/api/day/{monday}").get_json()
        assert body["lectures"], "Monday should have classes"
        assert all(l["absence_id"] is None for l in body["lectures"])

        first = body["lectures"][0]
        client.post("/api/absences", json={
            "date": monday, "subject_id": first["subject_id"], "start": first["start"],
        })

        after = client.get(f"/api/day/{monday}").get_json()
        assert after["lectures"][0]["absence_id"] is not None

    def test_a_day_far_out_is_still_plannable_but_outside_the_horizon(self, client, app):
        """You can commit to skipping in November; it just doesn't spend from
        the budget being shown until the checkpoint before it has passed."""
        confirm_timetable(client)
        set_end(client)
        client.post("/checkpoints", data={"on_date": "2026-09-15", "label": "Mid-sem"})

        near = client.get(f"/api/day/{(TODAY + timedelta(days=1)).isoformat()}").get_json()
        far = client.get("/api/day/2026-11-02").get_json()

        assert near["in_horizon"] is True
        assert far["in_horizon"] is False
        assert far["in_semester"] is True

    def test_a_bad_date_is_refused(self, client):
        assert client.get("/api/day/not-a-date").status_code == 400

    def test_the_sheet_carries_a_label_a_verdict_and_budgets(self, client):
        """The sheet is where the decision gets taken, and it was the one
        surface with no budgets on it — and an ISO date for a title."""
        confirm_timetable(client)
        set_end(client)
        body = client.get(f"/api/day/{(TODAY + timedelta(days=1)).isoformat()}").get_json()

        assert body["label"] == "Monday 17 Aug"
        assert body["plan"]["verdict"] in {"skip", "partial", "go", "planned", "off"}
        assert all(l["budget"] is not None for l in body["lectures"])
        assert all(l["verdict"] for l in body["lectures"])

    def test_lectures_sharing_a_slot_are_flagged(self, client):
        """The golden timetable runs two subjects at 13:00 on Thursday."""
        confirm_timetable(client)
        set_end(client)
        thursday = TODAY + timedelta(days=(3 - TODAY.weekday()) % 7 or 7)
        body = client.get(f"/api/day/{thursday.isoformat()}").get_json()
        assert any(l["same_slot"] for l in body["lectures"])

    def test_a_hand_edited_time_still_matches_an_existing_absence(self, client, app):
        """The portal prints "08:00:01"; an <input type="time"> can only give
        back "08:00". Matching on the exact second would let a timetable edit
        silently orphan an absence, which would then quietly stop counting."""
        confirm_timetable(client)
        set_end(client)
        monday = (TODAY + timedelta(days=1)).isoformat()
        sheet = client.get(f"/api/day/{monday}").get_json()
        first = sheet["lectures"][0]

        # Book against the truncated time the browser would actually send.
        resp = client.post("/api/absences", json={
            "date": monday,
            "subject_id": first["subject_id"],
            "start": first["start"][:5] + ":00",
        })
        assert resp.status_code == 200, resp.get_json()

        after = client.get(f"/api/day/{monday}").get_json()
        assert after["lectures"][0]["absence_id"] is not None


class TestOverlappingAbsences:
    """The three tiers overlap by design — a whole-day row and a single-class
    row on the same day both cover that class. Counting rows instead of the
    lectures they resolve to charged the budget twice for one lecture."""

    def _busy_day(self, client):
        """The first upcoming date the timetable actually has classes on."""
        for offset in range(1, 15):
            day = TODAY + timedelta(days=offset)
            sheet = client.get(f"/api/day/{day.isoformat()}").get_json()
            if sheet["lectures"]:
                return day, sheet
        raise AssertionError("no day with lectures in the next fortnight")

    def test_a_class_inside_a_planned_day_is_not_charged_twice(self, client):
        confirm_timetable(client)
        set_end(client)
        day, sheet = self._busy_day(client)
        held = len(sheet["lectures"])

        whole = client.post("/api/absences", json={"date": day.isoformat()}).get_json()
        assert whole["overall_planned"] == held

        first = sheet["lectures"][0]
        both = client.post("/api/absences", json={
            "date": day.isoformat(),
            "subject_id": first["subject_id"],
            "start": first["start"],
        }).get_json()

        # The extra row says nothing new: that lecture was already written off.
        assert both["overall_planned"] == held
        assert both["overall_budget"] == whole["overall_budget"]

    def test_every_surface_counts_the_same_day_the_same_way(self, client):
        """The calendar used to count rows while the strip counted lectures, so
        one whole-day plan read as 1 in one place and 7 in another."""
        confirm_timetable(client)
        set_end(client)
        day, sheet = self._busy_day(client)
        held = len(sheet["lectures"])
        client.post("/api/absences", json={"date": day.isoformat()})

        calendar = client.get("/calendar").get_data(as_text=True)
        badge = re.search(
            rf'data-date="{day.isoformat()}"[\s\S]{{0,400}}?cal__count mono">(\d+)<',
            calendar,
        )
        assert badge and int(badge.group(1)) == held

        plan = client.get("/plan").get_data(as_text=True)
        grid = re.search(
            rf'data-date="{day.isoformat()}"[\s\S]{{0,600}}?cal__count mono">(\d+)<',
            plan,
        )
        assert grid and int(grid.group(1)) == held

    def test_simulating_a_lecture_already_committed_costs_nothing(self, client):
        confirm_timetable(client)
        set_end(client)
        day, sheet = self._busy_day(client)
        first = sheet["lectures"][0]
        client.post("/api/absences", json={
            "date": day.isoformat(),
            "subject_id": first["subject_id"],
            "start": first["start"],
        })

        committed = client.post("/api/simulate", json={"absences": []}).get_json()
        again = client.post("/api/simulate", json={"absences": [
            {"date": day.isoformat(), "subject_id": first["subject_id"],
             "start": first["start"]},
        ]}).get_json()
        assert again["overall_planned"] == committed["overall_planned"]


class TestTimetableSaveHonesty:
    """`save_timetable` blanks the current version before writing, so a save
    that stores nothing doesn't leave the grid alone — it wipes it."""

    def test_a_grid_of_unowned_subjects_is_refused_not_applied(self, client, app):
        confirm_timetable(client)
        set_end(client)
        with app.app_context():
            before = db.session.query(TimetableSlot).filter_by(kind="class").count()
        assert before

        resp = client.put("/api/timetable", json={"blocks": [
            {"kind": "class", "weekday": 0, "start": "09:00", "end": "10:00",
             "subject_id": 999999},
        ]})
        assert resp.status_code == 422

        with app.app_context():
            assert db.session.query(TimetableSlot).filter_by(kind="class").count() == before

    def test_the_reported_count_is_what_was_stored(self, client, app):
        """Not what was asked for: an unowned block is dropped on the way in,
        and saying it saved would be a lie the next page load contradicts."""
        confirm_timetable(client)
        blocks = [
            {"kind": "class", "weekday": 0, "start": "09:00", "end": "10:00",
             "subject_id": 999999},
        ]
        with app.app_context():
            mine = db.session.query(Subject).first().id
        blocks.append({"kind": "class", "weekday": 1, "start": "09:00",
                       "end": "10:00", "subject_id": mine})

        body = client.put("/api/timetable", json={"blocks": blocks}).get_json()
        assert body["classes"] == 1
        with app.app_context():
            assert db.session.query(TimetableSlot).filter_by(kind="class").count() == 1


class TestBatchAbsences:
    """"Leave after 12:00" is one decision, so it lands as one transaction."""

    def _lectures_on_a_busy_day(self, client):
        for offset in range(1, 15):
            day = TODAY + timedelta(days=offset)
            sheet = client.get(f"/api/day/{day.isoformat()}").get_json()
            if len(sheet["lectures"]) >= 2:
                return day, sheet["lectures"]
        raise AssertionError("no day with two lectures in the next fortnight")

    def test_a_batch_commits_every_lecture_and_returns_their_ids(self, client, app):
        confirm_timetable(client)
        set_end(client)
        day, lectures = self._lectures_on_a_busy_day(client)
        wanted = [{"subject_id": l["subject_id"], "start": l["start"]}
                  for l in lectures[:2]]

        body = client.post("/api/absences/batch", json={
            "date": day.isoformat(), "lectures": wanted,
        }).get_json()

        assert body["overall_planned"] == 2
        for item in wanted:
            assert body["absence_ids"][f"{item['subject_id']}|{item['start']}"]
        with app.app_context():
            assert db.session.query(PlannedAbsence).count() == 2

    def test_one_bad_lecture_rolls_the_whole_batch_back(self, client, app):
        """A half-applied "leave after 12:00" is a plan the user never made."""
        confirm_timetable(client)
        set_end(client)
        day, lectures = self._lectures_on_a_busy_day(client)

        resp = client.post("/api/absences/batch", json={
            "date": day.isoformat(),
            "lectures": [
                {"subject_id": lectures[0]["subject_id"], "start": lectures[0]["start"]},
                {"subject_id": 999999, "start": lectures[0]["start"]},
            ],
        })
        assert resp.status_code == 404
        with app.app_context():
            assert db.session.query(PlannedAbsence).count() == 0

    def test_an_empty_batch_is_refused(self, client):
        confirm_timetable(client)
        set_end(client)
        assert client.post("/api/absences/batch", json={
            "date": (TODAY + timedelta(days=1)).isoformat(), "lectures": [],
        }).status_code == 400


class TestHolidayName:
    def test_a_name_is_trimmed_to_the_column(self, client, app):
        """SQLite doesn't enforce String(120), so an unbounded name would be
        stored in full and only blow up on a database that does."""
        confirm_timetable(client)
        set_end(client)
        day = TODAY + timedelta(days=2)
        client.post("/api/calendar/day", json={
            "date": day.isoformat(), "kind": "holiday", "name": "x" * 500,
        })
        with app.app_context():
            assert len(db.session.query(Holiday).one().name) == 120
