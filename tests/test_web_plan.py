"""Phase 2 web layer: timetable confirmation, calendar, wallet, day strip.

The fixture drives the whole advanced-mode setup the way a user would: upload
the real PDF, accept the inferred timetable, set the semester end date.
"""
from datetime import date, timedelta
from pathlib import Path

import pytest

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
    client.post("/register", data={"email": "m@example.com", "password": "password123", "username": "muser", "confirm_password": "password123"})
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

    def test_today_refuses_a_date_outside_this_week(self, client):
        """The arrows are a week navigator; anything further out is /plan's job,
        so an out-of-range date falls back to today rather than 404ing."""
        confirm_timetable(client)
        set_end(client)
        html = client.get("/?date=2026-11-20").get_data(as_text=True)
        assert "2026-11-20" not in html

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
        assert verdicts <= {"skip", "partial", "go", "off"}
        # Weekends carry no classes in this timetable.
        sunday = next(d for d in body["days"] if date.fromisoformat(d["date"]).weekday() == 6)
        assert sunday["verdict"] == "off"

    def test_a_committed_day_stops_asking_you_to_decide_it(self, client):
        """The budget already paid for those lectures. Re-charging it read back
        as "part skip" on a day that was settled."""
        confirm_timetable(client)
        set_end(client)
        client.post("/api/absences", json={"date": "2026-08-17"})

        body = client.post("/api/simulate", json={"absences": []}).get_json()
        day = next(d for d in body["days"] if d["date"] == "2026-08-17")
        assert day["verdict"] == "skip"
        assert "Already planning to miss" in day["reason"]


class TestPlanPage:
    def test_not_ready_still_shows_attendance_and_the_next_step(self, client):
        """A missing timetable costs you the planning half, not your ledger."""
        html = client.get("/plan").get_data(as_text=True)
        assert "Confirm timetable" in html
        assert "Attended" in html

    def test_ready_shows_the_strip_and_the_plan_controls(self, client):
        confirm_timetable(client)
        set_end(client)
        html = client.get("/plan").get_data(as_text=True)
        assert "day-strip" in html
        assert "Committed absences" in html
        assert "What if I skip one subject?" in html

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
        assert "verdict__count" in html

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
        other.post("/register", data={
            "email": "other@example.com", "username": "other",
            "password": "password123", "confirm_password": "password123"})
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
