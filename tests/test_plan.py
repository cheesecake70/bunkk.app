"""Advanced-mode engine: inference, projection, wallet, day verdicts.

The load-bearing test here is `test_a_skip_verdict_never_breaks_a_limit`: if the
app ever says a day is skippable when skipping it drops a subject below its
line, the product has failed at its one job. It is asserted over generated data,
not just examples.
"""
from datetime import date, time, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from attendance_engine import (
    CalendarRules,
    Counts,
    DayVerdict,
    LectureRow,
    PlannedLecture,
    Slot,
    Verdict,
    build_wallet,
    count_by_subject,
    day_plans,
    expand,
    infer_slots,
    simulate,
)
from report_parser import parse_pdf

MON, TUE, WED, THU, FRI = range(5)


# ---------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------


def rows_for(subject_id, weekday, start, weeks, first=date(2026, 7, 6)):
    """`weeks` consecutive occurrences of one weekly slot."""
    base = first + timedelta(days=(weekday - first.weekday()) % 7)
    return [
        LectureRow(subject_id, base + timedelta(weeks=w), start,
                   time(start.hour + 1, 0))
        for w in range(weeks)
    ]


class TestInference:
    def test_a_weekly_pattern_becomes_a_slot(self):
        candidates = infer_slots(rows_for(1, MON, time(9, 0), weeks=4))
        assert len(candidates) == 1
        c = candidates[0]
        assert c.slot == Slot(1, MON, time(9, 0), time(10, 0))
        assert (c.occurrences, c.weeks_seen) == (4, 4)
        assert c.is_confident

    def test_a_one_off_is_offered_but_not_confident(self):
        candidates = infer_slots(rows_for(1, MON, time(9, 0), weeks=1))
        assert len(candidates) == 1
        assert not candidates[0].is_confident

    def test_two_subjects_may_share_one_slot(self):
        """Thu 13:00 genuinely holds both DBMS Theory and DS Theory."""
        rows = rows_for(1, THU, time(13, 0), 4) + rows_for(2, THU, time(13, 0), 3)
        candidates = infer_slots(rows)
        assert len(candidates) == 2
        assert {c.slot.subject_id for c in candidates} == {1, 2}

    def test_the_usual_end_time_wins(self):
        rows = rows_for(1, MON, time(9, 0), 3)
        rows.append(LectureRow(1, date(2026, 8, 3), time(9, 0), time(9, 45)))
        assert infer_slots(rows)[0].slot.end_time == time(10, 0)

    def test_real_ledger_infers_a_five_day_week(self):
        report = parse_pdf("tests/golden/detailed_jul_aug.pdf")
        codes = {}
        rows = []
        for lec in report.lectures:
            sid = codes.setdefault(lec.course_key, len(codes) + 1)
            rows.append(LectureRow(sid, lec.on_date, lec.start_time, lec.end_time))

        confident = [c for c in infer_slots(rows) if c.is_confident]
        assert len(confident) == 32          # 33 patterns, one seen only once
        assert {c.slot.weekday for c in confident} == {MON, TUE, WED, THU, FRI}


# ---------------------------------------------------------------------------
# Expansion over the calendar
# ---------------------------------------------------------------------------


class TestExpand:
    slots = [Slot(1, MON, time(9, 0), time(10, 0)),
             Slot(2, WED, time(11, 0), time(12, 0))]

    def test_counts_each_weekday_occurrence(self):
        occ = expand(self.slots, date(2026, 8, 17), date(2026, 8, 30))
        assert count_by_subject(occ) == {1: 2, 2: 2}

    def test_holidays_remove_a_day_entirely(self):
        rules = CalendarRules(holidays=frozenset({date(2026, 8, 17)}))
        occ = expand(self.slots, date(2026, 8, 17), date(2026, 8, 23), rules)
        assert count_by_subject(occ) == {2: 1}

    def test_a_swap_day_runs_another_weekdays_timetable(self):
        """Friday running Monday's timetable is a real college habit."""
        rules = CalendarRules(swaps={date(2026, 8, 21): MON})
        occ = expand(self.slots, date(2026, 8, 21), date(2026, 8, 21), rules)
        assert count_by_subject(occ) == {1: 1}

    def test_empty_range_is_empty(self):
        assert expand(self.slots, date(2026, 9, 1), date(2026, 8, 1)) == []


# ---------------------------------------------------------------------------
# The wallet
# ---------------------------------------------------------------------------


def subject_row(sid=1, code="CN", limit=70, present=0, absent=0, unknown=0,
                unreported=0, remaining=0, planned=0):
    return dict(id=sid, code=code, canonical_name=code, lecture_type="Theory",
                limit=limit, counts=Counts(present, absent, unknown),
                unreported=unreported, remaining=remaining,
                planned_absences=planned)


class TestWallet:
    def test_worked_example(self):
        """14 present, 2 absent, 0 pending, 10 remaining, limit 70%.
        T = 26, so you must end on ≥ 18.2 → 19 attended. You'd have 24 if you
        attended everything, so 5 are spendable."""
        w = build_wallet([subject_row(present=14, absent=2, remaining=10)], 70)
        s = w.subjects[0]
        assert s.projected_total == 26
        assert s.budget == 5
        assert 100 * (14 + 10 - 5) >= 70 * 26        # spending all 5 is safe
        assert 100 * (14 + 10 - 6) < 70 * 26         # one more is not

    def test_unreported_lectures_shrink_the_budget(self):
        """Lectures held since the last upload can't be attended — worst case
        they were missed, so they must count against you."""
        without = build_wallet([subject_row(present=14, absent=2, remaining=10)], 70)
        with_gap = build_wallet(
            [subject_row(present=14, absent=2, remaining=10, unreported=4)], 70)
        assert with_gap.subjects[0].budget < without.subjects[0].budget

    def test_pending_lectures_count_against_you(self):
        """4 pending on top of the worked example: T grows 26 → 30, so the
        budget falls 5 → 3 (attend 7 of 10 left ⇒ 21/30, exactly 70%)."""
        w = build_wallet([subject_row(present=14, absent=2, unknown=4, remaining=10)], 70)
        assert w.subjects[0].projected_total == 30
        assert w.subjects[0].budget == 3
        assert 100 * (14 + 10 - 3) >= 70 * 30
        assert 100 * (14 + 10 - 4) < 70 * 30

    def test_planned_absences_are_spent_first(self):
        base = build_wallet([subject_row(present=14, absent=2, remaining=10)], 70)
        after = build_wallet(
            [subject_row(present=14, absent=2, remaining=10, planned=3)], 70)
        assert after.subjects[0].budget == base.subjects[0].budget - 3

    def test_budget_never_exceeds_what_remains(self):
        w = build_wallet([subject_row(present=100, absent=0, remaining=2)], 70)
        assert w.subjects[0].budget == 2

    def test_unreachable_limit_is_danger(self):
        """5 present, 20 absent, 2 left: even perfect attendance ends at 7/27."""
        w = build_wallet([subject_row(present=5, absent=20, remaining=2)], 70)
        assert w.subjects[0].verdict is Verdict.DANGER
        assert w.subjects[0].budget == 0

    def test_overall_limit_caps_every_subject(self):
        rows = [subject_row(1, "A", present=9, absent=1, remaining=5),
                subject_row(2, "B", present=6, absent=4, remaining=5)]
        w = build_wallet(rows, overall_limit=95)
        assert w.overall_budget == 0
        assert all(s.budget == 0 for s in w.subjects)
        assert all(s.verdict is not Verdict.SAFE for s in w.subjects)

    def test_tightest_subject_is_named(self):
        rows = [subject_row(1, "Roomy", present=20, absent=0, remaining=10),
                subject_row(2, "Tight", present=7, absent=3, remaining=2)]
        assert build_wallet(rows, 70).tightest.code == "Tight"

    @given(
        present=st.integers(0, 60), absent=st.integers(0, 60),
        unknown=st.integers(0, 20), unreported=st.integers(0, 20),
        remaining=st.integers(0, 40), limit=st.integers(1, 99),
    )
    @settings(max_examples=400)
    def test_budget_is_exactly_maximal(self, present, absent, unknown,
                                       unreported, remaining, limit):
        w = build_wallet([subject_row(
            present=present, absent=absent, unknown=unknown,
            unreported=unreported, remaining=remaining, limit=limit)], limit)
        s = w.subjects[0]
        total = s.projected_total
        if s.budget > 0:
            assert 100 * (present + remaining - s.budget) >= limit * total
        if s.budget < remaining:
            assert 100 * (present + remaining - s.budget - 1) < limit * total


# ---------------------------------------------------------------------------
# Day verdicts
# ---------------------------------------------------------------------------


def lecture(code, sid, hour, day=date(2026, 8, 17)):
    return PlannedLecture(sid, code, day, time(hour, 0), time(hour + 1, 0))


class TestDayVerdicts:
    def _wallet(self, budgets, overall):
        rows = []
        for sid, (code, budget) in enumerate(budgets.items(), start=1):
            # present/remaining chosen so the derived budget equals `budget`
            rows.append(subject_row(sid, code, limit=0, present=0,
                                    remaining=budget))
        w = build_wallet(rows, overall_limit=0)
        return w

    def test_no_classes_is_off(self):
        w = self._wallet({"CN": 3}, 3)
        plans = day_plans({}, w, [date(2026, 8, 22)])
        assert plans[0].verdict is DayVerdict.OFF

    def test_a_day_inside_budget_is_skippable(self):
        w = self._wallet({"CN": 3}, 5)
        day = date(2026, 8, 17)
        plans = day_plans({day: [lecture("CN", 1, 9), lecture("CN", 1, 10)]}, w, [day])
        assert plans[0].verdict is DayVerdict.SKIP
        assert "2× CN" in plans[0].reason

    def test_a_day_beyond_budget_is_a_must_go(self):
        w = self._wallet({"CN": 0}, 0)
        day = date(2026, 8, 17)
        plans = day_plans({day: [lecture("CN", 1, 9)]}, w, [day])
        assert plans[0].verdict is DayVerdict.GO
        assert "CN" in plans[0].reason

    def test_partial_day_offers_leaving_early(self):
        """Budget covers one lecture, so the last one is droppable."""
        w = self._wallet({"CN": 1}, 1)
        day = date(2026, 8, 17)
        plans = day_plans(
            {day: [lecture("CN", 1, 9), lecture("CN", 1, 10)]}, w, [day])
        assert plans[0].verdict is DayVerdict.PARTIAL
        assert plans[0].leave_after == time(10, 0)
        assert "Leave after 10:00" in plans[0].reason

    def test_horizon_is_respected(self):
        w = self._wallet({"CN": 3}, 3)
        horizon = [date(2026, 8, 17) + timedelta(days=i) for i in range(14)]
        assert len(day_plans({}, w, horizon)) == 14

    @given(
        budget=st.integers(0, 6),
        overall=st.integers(0, 6),
        lecture_count=st.integers(1, 5),
    )
    def test_a_skip_verdict_never_breaks_a_limit(self, budget, overall, lecture_count):
        """The product's one unforgivable failure, asserted directly."""
        w = self._wallet({"CN": budget}, overall)
        effective = w.subjects[0].budget
        day = date(2026, 8, 17)
        lectures = [lecture("CN", 1, 9 + i) for i in range(lecture_count)]
        plan = day_plans({day: lectures}, w, [day])[0]

        if plan.verdict is DayVerdict.SKIP:
            assert lecture_count <= effective
            assert lecture_count <= w.overall_budget


class TestSimulator:
    def test_over_commitment_names_what_breaks(self):
        rows = [subject_row(1, "CN", present=7, absent=3, remaining=5, planned=5),
                subject_row(2, "OR", present=9, absent=1, remaining=5, planned=0)]
        result = simulate(rows, overall_limit=70)
        assert not result.is_safe
        assert result.breaks == ["CN"]           # 7/20 = 35% if all 5 are missed

    def test_a_safe_plan_reports_safe(self):
        rows = [subject_row(1, "CN", present=14, absent=2, remaining=10, planned=2)]
        result = simulate(rows, overall_limit=70)
        assert result.is_safe
        assert result.wallet.subjects[0].budget == 3      # 5 minus the 2 committed

    def test_planning_shows_where_you_land(self):
        rows = [subject_row(1, "CN", present=14, absent=2, remaining=10, planned=4)]
        plan = simulate(rows, 70).wallet.subjects[0]
        # 14 + (10 - 4) = 20 attended out of 26
        assert plan.projected_worst_pct == pytest.approx(2000 / 26)
