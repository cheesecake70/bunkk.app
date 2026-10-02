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
    BreakSpan,
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
    skip_ladder,
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


def lecture(code, sid, hour, day=date(2026, 8, 17), planned=False):
    return PlannedLecture(sid, code, day, time(hour, 0), time(hour + 1, 0),
                          planned=planned)


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

    def test_a_fully_committed_day_reads_as_planned(self):
        """The wallet already paid for those lectures when they were committed;
        asking it to pay again turned a settled day back into "part skip".

        PLANNED rather than SKIP, because "skip" is advice — and advice about a
        decision already taken is at best noise, at worst a green light over an
        over-spend."""
        w = self._wallet({"CN": 0}, 0)          # budget spent on this very day
        day = date(2026, 8, 17)
        plans = day_plans(
            {day: [lecture("CN", 1, 9, planned=True),
                   lecture("CN", 1, 10, planned=True)]}, w, [day])
        assert plans[0].verdict is DayVerdict.PLANNED
        assert plans[0].whole_day
        assert plans[0].planned_count == 2
        assert "Skipping the whole day" in plans[0].reason

    def test_a_planned_day_that_breaks_a_limit_says_so(self):
        """The commitment stands; what changes is that the day stops calling
        itself safe. This is the walkthrough's headline bug: a whole day written
        off against an empty budget still read green."""
        rows = [subject_row(1, "CN", limit=70, present=0, absent=9,
                            remaining=1, planned=1)]
        w = build_wallet(rows, overall_limit=70)
        day = date(2026, 8, 17)

        plan = day_plans({day: [lecture("CN", 1, 9, planned=True)]}, w, [day])[0]
        assert plan.verdict is DayVerdict.PLANNED
        assert plan.over_budget
        assert "Over budget: CN" in plan.reason

    def test_partial_commit_with_nothing_left_to_cut_is_planned(self):
        """Taking the app's own "leave after 10:00" advice used to flip the day
        to MUST GO, which reads as the advice being withdrawn."""
        w = self._wallet({"CN": 0}, 0)
        day = date(2026, 8, 17)
        plans = day_plans(
            {day: [lecture("CN", 1, 9), lecture("CN", 1, 10, planned=True)]},
            w, [day])
        assert plans[0].verdict is DayVerdict.PLANNED
        assert plans[0].planned_count == 1
        assert plans[0].leave_after == time(10, 0)
        assert "must-attend" in plans[0].reason

    def test_a_planned_morning_reads_as_arriving_late(self):
        w = self._wallet({"CN": 0}, 0)
        day = date(2026, 8, 17)
        plans = day_plans(
            {day: [lecture("CN", 1, 9, planned=True), lecture("CN", 1, 10)]},
            w, [day])
        assert plans[0].verdict is DayVerdict.PLANNED
        assert plans[0].arrive_at == time(10, 0)
        assert "Arriving by 10:00" in plans[0].reason

    def test_partial_commit_with_room_left_keeps_advising(self):
        """A commitment doesn't silence the day while budget remains."""
        w = self._wallet({"CN": 1}, 1)
        day = date(2026, 8, 17)
        plans = day_plans(
            {day: [lecture("CN", 1, 9, planned=True), lecture("CN", 1, 10)]},
            w, [day])
        assert plans[0].verdict is DayVerdict.SKIP
        assert plans[0].planned_count == 1

    def test_committed_lectures_are_not_charged_for_twice(self):
        """One of two lectures is already committed; one lecture of budget is
        left, so the rest of the day is skippable outright."""
        w = self._wallet({"CN": 1}, 1)
        day = date(2026, 8, 17)
        plans = day_plans(
            {day: [lecture("CN", 1, 9, planned=True), lecture("CN", 1, 10)]},
            w, [day])
        assert plans[0].verdict is DayVerdict.SKIP
        assert plans[0].skippable_codes == ["CN"]

    def test_a_committed_class_never_becomes_the_one_you_stay_for(self):
        w = self._wallet({"CN": 1}, 1)
        day = date(2026, 8, 17)
        plans = day_plans(
            {day: [lecture("CN", 1, 9),
                   lecture("CN", 1, 10, planned=True),
                   lecture("CN", 1, 11)]}, w, [day])
        assert plans[0].verdict is DayVerdict.PARTIAL
        # 09:00 is the last class you actually attend — the 10:00 you already
        # wrote off cannot be what keeps you there.
        assert plans[0].leave_after == time(10, 0)

    def test_a_cut_across_a_break_beats_one_that_leaves_you_sitting(self):
        """Two cuts skip two lectures each. Only one of them also gets you out
        before an hour of nothing, and that hour is the whole point."""
        w = self._wallet({"CN": 2}, 2)
        day = date(2026, 8, 17)
        # 09, 10, then a 12–13 break, then 13, 14. Leaving after 10:00 skips
        # 13:00 and 14:00 *and* the break; arriving at 13:00 skips 09 and 10.
        lectures = [lecture("CN", 1, 9), lecture("CN", 1, 10),
                    lecture("CN", 1, 13), lecture("CN", 1, 14)]
        breaks = {day: [BreakSpan(time(12, 0), time(13, 0), "Lunch")]}

        plan = day_plans({day: lectures}, w, [day], breaks)[0]
        assert plan.verdict is DayVerdict.PARTIAL
        assert plan.leave_after == time(11, 0)
        assert plan.covers_break == "Lunch"
        # 11:00 to 15:00 — the two lectures, plus the lunch hour between them.
        assert plan.freed_minutes == 240
        assert "Lunch" in plan.reason

    def test_without_breaks_the_verdict_is_what_it_always_was(self):
        w = self._wallet({"CN": 1}, 1)
        day = date(2026, 8, 17)
        lectures = [lecture("CN", 1, 9), lecture("CN", 1, 10)]

        bare = day_plans({day: lectures}, w, [day])[0]
        empty = day_plans({day: lectures}, w, [day], {day: []})[0]
        assert bare.verdict is empty.verdict is DayVerdict.PARTIAL
        assert bare.leave_after == empty.leave_after == time(10, 0)
        assert bare.covers_break is None

    def test_the_sentence_describes_the_cut_it_actually_chose(self):
        """The wording used to be picked before the winner was, so a day whose
        best move was arriving late could still be told to leave early."""
        w = self._wallet({"CN": 1, "OS": 1}, 2)
        day = date(2026, 8, 17)
        # OS has room for one of its two, so the whole day never fits. Dropping
        # the morning (CN 09:00 + OS 10:00) frees two hours; dropping the last
        # lecture frees one. Arriving late wins, and the sentence has to say so.
        lectures = [lecture("CN", 1, 9), lecture("OS", 2, 10), lecture("OS", 2, 11)]

        plan = day_plans({day: lectures}, w, [day])[0]
        assert plan.verdict is DayVerdict.PARTIAL
        assert plan.arrive_at == time(11, 0)
        assert plan.leave_after is None
        assert "Arrive by 11:00" in plan.reason
        assert plan.skippable_codes == ["CN", "OS"]
        assert [l.code for l in plan.skippable] == ["CN", "OS"]

    def test_a_break_never_reaches_the_budget(self):
        """It changes where you cut the day, never how much the cut costs."""
        w = self._wallet({"CN": 1}, 1)
        day = date(2026, 8, 17)
        lectures = [lecture("CN", 1, 9), lecture("CN", 1, 11)]
        breaks = {day: [BreakSpan(time(10, 0), time(11, 0), "Break")]}

        plan = day_plans({day: lectures}, w, [day], breaks)[0]
        assert plan.verdict is DayVerdict.PARTIAL
        assert len(plan.skippable) == 1          # one lecture of budget, one lecture

    def test_horizon_is_respected(self):
        w = self._wallet({"CN": 3}, 3)
        horizon = [date(2026, 8, 17) + timedelta(days=i) for i in range(14)]
        assert len(day_plans({}, w, horizon)) == 14

    @given(
        budget=st.integers(0, 6),
        overall=st.integers(0, 6),
        lecture_count=st.integers(1, 5),
        committed=st.lists(st.booleans(), min_size=5, max_size=5),
    )
    def test_a_skip_verdict_never_breaks_a_limit(self, budget, overall,
                                                 lecture_count, committed):
        """The product's one unforgivable failure, asserted directly.

        Commitments are in the strategy because they are how the failure came
        back: a day whose lectures were all committed returned SKIP no matter
        what the budget said. SKIP now only ever describes lectures still
        undecided, and it has to fit them."""
        w = self._wallet({"CN": budget}, overall)
        effective = w.subjects[0].budget
        day = date(2026, 8, 17)
        lectures = [lecture("CN", 1, 9 + i, planned=committed[i])
                    for i in range(lecture_count)]
        plan = day_plans({day: lectures}, w, [day])[0]
        pending = [l for l in lectures if not l.planned]

        # A day with nothing left to decide is never advice.
        assert not (plan.verdict is DayVerdict.SKIP and not pending)

        if plan.verdict is DayVerdict.SKIP:
            assert len(pending) <= effective
            assert len(pending) <= w.overall_budget
        if plan.verdict is DayVerdict.PLANNED:
            assert any(l.planned for l in lectures)
        assert plan.planned_count == sum(1 for l in lectures if l.planned)


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


class TestSkipLadder:
    """"If I skip DBMS, what does the 1st, 2nd, 3rd cost me?" """

    def _rows(self):
        # 14 present, 2 absent, 10 remaining, limit 70% -> budget 5.
        return [subject_row(sid=1, code="DBMS", limit=70, present=14, absent=2,
                            remaining=10),
                subject_row(sid=2, code="OR", limit=70, present=20, absent=0,
                            remaining=10)]

    def test_each_rung_matches_committing_that_many_absences(self):
        """The ladder must agree with actually planning n absences, or it is
        advertising an outcome the app won't deliver."""
        rows = self._rows()
        for rung in skip_ladder(rows, overall_limit=75, subject_id=1):
            committed = [
                {**r, "planned_absences": r["planned_absences"] + rung.n}
                if r["id"] == 1 else r
                for r in rows
            ]
            wallet = build_wallet(committed, overall_limit=75)
            plan = wallet.by_id()[1]
            assert rung.subject_pct == plan.projected_worst_pct
            assert rung.subject_budget_left == plan.budget

    def test_it_never_mutates_the_rows_it_is_given(self):
        """`wallet_rows` hands out shared dicts; mutating them would corrupt
        every later rung and the caller's wallet with it."""
        rows = self._rows()
        before = [dict(r) for r in rows]
        skip_ladder(rows, overall_limit=75, subject_id=1)
        assert [dict(r) for r in rows] == before

    def test_the_ladder_stops_at_what_remains(self):
        rows = [subject_row(sid=1, present=10, absent=0, remaining=2)]
        rungs = skip_ladder(rows, overall_limit=75, subject_id=1)
        assert [r.n for r in rungs] == [1, 2]

    def test_it_shows_the_cliff_and_a_step_past_it(self):
        rungs = skip_ladder(self._rows(), overall_limit=75, subject_id=1)
        unsafe = [r for r in rungs if not r.is_safe]
        assert unsafe, "a ladder that never breaks tells you nothing"
        assert rungs[-1].n > unsafe[0].n, "should show at least one rung past the cliff"

    def test_a_subject_already_broken_does_not_blame_this_choice(self):
        """Someone under water everywhere would otherwise see the same codes
        against every rung, which says nothing about the decision at hand."""
        rows = [subject_row(sid=1, code="DBMS", limit=70, present=1, absent=9,
                            remaining=1),
                subject_row(sid=2, code="OR", limit=70, present=0, absent=10,
                            remaining=1)]
        for rung in skip_ladder(rows, overall_limit=75, subject_id=1):
            assert "OR" not in rung.breaks

    def test_an_unknown_subject_has_no_ladder(self):
        assert skip_ladder(self._rows(), overall_limit=75, subject_id=99) == []


class TestHorizonMonotonicity:
    """Checkpoints shorten the window every projection runs to. That must only
    ever be the more conservative reading — if a nearer deadline could hand you
    a bigger budget, the feature would be actively dangerous."""

    @given(
        present=st.integers(0, 60), absent=st.integers(0, 60),
        unknown=st.integers(0, 20), unreported=st.integers(0, 20),
        remaining=st.integers(0, 40), shrink=st.integers(0, 40),
        limit=st.integers(1, 99),
    )
    @settings(max_examples=400)
    def test_a_shorter_horizon_never_grows_the_budget(
        self, present, absent, unknown, unreported, remaining, shrink, limit
    ):
        def budget(r):
            rows = [subject_row(present=present, absent=absent, unknown=unknown,
                                unreported=unreported, remaining=r, limit=limit)]
            return build_wallet(rows, overall_limit=limit).subjects[0].budget

        assert budget(max(0, remaining - shrink)) <= budget(remaining)
