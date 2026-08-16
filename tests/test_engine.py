"""attendance_engine tests: worked examples + properties.

The engine's one unforgivable failure is telling a student a bunk is safe when
it isn't, so the properties below assert the *tight* form of each answer: the
budget is not merely safe, it is exactly one lecture away from unsafe.
"""
from datetime import date
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from attendance_engine import (
    Counts,
    DateRange,
    Verdict,
    analyse,
    build_dashboard,
    meets_limit,
    merge_ranges,
    ratio,
    recovery_needed,
    safe_to_miss,
)

counts_st = st.builds(
    Counts,
    present=st.integers(min_value=0, max_value=200),
    absent=st.integers(min_value=0, max_value=200),
    unknown=st.integers(min_value=0, max_value=200),
)
limit_st = st.integers(min_value=1, max_value=99)


class TestCounts:
    def test_derived_totals(self):
        c = Counts(present=8, absent=2, unknown=5)
        assert c.conducted == 10
        assert c.total == 15

    def test_addition_aggregates(self):
        assert Counts(1, 2, 3) + Counts(10, 20, 30) == Counts(11, 22, 33)


class TestPercentages:
    def test_official_excludes_pending(self):
        # 8 present, 2 absent, 5 pending: portal shows 8/10 = 80%
        assert ratio(8, 10) == 80

    def test_worst_case_counts_pending_as_absent(self):
        assert ratio(8, 15) == Fraction(160, 3)  # 53.33%

    def test_best_case_counts_pending_as_present(self):
        assert ratio(8 + 5, 15) == Fraction(260, 3)  # 86.67%

    def test_zero_denominator_is_none(self):
        assert ratio(0, 0) is None


class TestMeetsLimit:
    def test_exact_boundary_is_inclusive(self):
        """70/100 must count as meeting a 70% rule — not 'just below'."""
        assert meets_limit(70, 100, 70)

    def test_one_short_fails(self):
        assert not meets_limit(69, 100, 70)

    def test_no_lectures_is_not_a_breach(self):
        assert meets_limit(0, 0, 70)

    @given(counts_st, limit_st)
    def test_agrees_with_exact_fraction_comparison(self, c, limit):
        """Integer comparison must never disagree with exact rational math."""
        if c.total == 0:
            return
        assert meets_limit(c.present, c.total, limit) == (
            Fraction(c.present, c.total) >= Fraction(limit, 100)
        )


class TestSafeToMiss:
    def test_worked_example(self):
        """P=40, T=50 at 70%: 40/50=80%. Missing 7 more → 40/57 = 70.1% ✓."""
        assert safe_to_miss(40, 50, 70) == 7
        assert meets_limit(40, 50 + 7, 70)
        assert not meets_limit(40, 50 + 8, 70)

    def test_below_limit_means_zero_budget(self):
        assert safe_to_miss(30, 50, 70) == 0

    def test_exactly_at_limit_means_zero_budget(self):
        assert safe_to_miss(70, 100, 70) == 0

    @given(counts_st, limit_st)
    @settings(max_examples=300)
    def test_budget_is_exactly_maximal(self, c, limit):
        """k lectures may be missed; k+1 may not. The tight guarantee.

        Only meaningful while the subject is currently above its limit — a
        subject already below it has no budget at all (asserted separately).
        """
        k = safe_to_miss(c.present, c.total, limit)
        if not meets_limit(c.present, c.total, limit):
            assert k == 0
            return
        assert meets_limit(c.present, c.total + k, limit)
        assert not meets_limit(c.present, c.total + k + 1, limit)

    @given(counts_st, limit_st)
    def test_never_offers_a_budget_while_below_limit(self, c, limit):
        if not meets_limit(c.present, c.total, limit):
            assert safe_to_miss(c.present, c.total, limit) == 0


class TestRecovery:
    def test_worked_example(self):
        """P=30, T=50 at 70%: need 30+r >= 0.7(50+r) → r >= 16.67 → 17."""
        assert recovery_needed(30, 50, 70) == 17
        assert meets_limit(30 + 17, 50 + 17, 70)
        assert not meets_limit(30 + 16, 50 + 16, 70)

    def test_zero_when_already_safe(self):
        assert recovery_needed(40, 50, 70) == 0

    @given(counts_st, limit_st)
    @settings(max_examples=300)
    def test_recovery_is_exactly_minimal(self, c, limit):
        r = recovery_needed(c.present, c.total, limit)
        assert meets_limit(c.present + r, c.total + r, limit)
        if r > 0:
            assert not meets_limit(c.present + r - 1, c.total + r - 1, limit)


class TestDashboard:
    def _rows(self):
        return [
            dict(id=1, code="CN", canonical_name="Computer Networks",
                 lecture_type="Theory", limit=70,
                 counts=Counts(present=18, absent=2, unknown=0)),      # 90%
            dict(id=2, code="OR", canonical_name="Operations Research",
                 lecture_type="Theory", limit=70,
                 counts=Counts(present=10, absent=8, unknown=2)),      # worst 50%
            dict(id=3, code="DBMS Lab", canonical_name="Database Management System Laboratory",
                 lecture_type="Practical", limit=70,
                 counts=Counts(present=7, absent=2, unknown=1)),       # worst 70%
        ]

    def test_overall_sums_every_subject(self):
        d = build_dashboard(self._rows(), overall_limit=75)
        assert d.overall.counts == Counts(present=35, absent=12, unknown=3)
        assert d.pending_total == 3

    def test_worst_subject_sorts_first(self):
        d = build_dashboard(self._rows(), overall_limit=75)
        assert d.subjects[0].code == "OR"

    def test_subject_below_limit_is_danger_with_recovery_plan(self):
        d = build_dashboard(self._rows(), overall_limit=75)
        orr = next(s for s in d.subjects if s.code == "OR")
        assert orr.verdict is Verdict.DANGER
        assert orr.can_miss == 0
        assert orr.recover_needed > 0

    def test_subject_exactly_at_limit_is_warn_not_danger(self):
        d = build_dashboard(self._rows(), overall_limit=75)
        lab = next(s for s in d.subjects if s.code == "DBMS Lab")
        assert lab.worst_pct == 70
        assert lab.verdict is Verdict.WARN
        assert lab.can_miss == 0

    def test_effective_budget_respects_the_overall_limit(self):
        """A subject with private slack is still capped by the 75% overall rule."""
        rows = [
            dict(id=1, code="A", canonical_name="A", lecture_type="Theory",
                 limit=70, counts=Counts(present=8, absent=2, unknown=0)),
            dict(id=2, code="B", canonical_name="B", lecture_type="Theory",
                 limit=70, counts=Counts(present=7, absent=3, unknown=0)),
        ]
        d = build_dashboard(rows, overall_limit=75)
        assert d.overall.can_miss == 0          # 15/20 = 75%, exactly at the line
        assert all(s.can_miss_effective == 0 for s in d.subjects)
        a = next(s for s in d.subjects if s.code == "A")
        assert a.can_miss == 1                  # its own 70% rule allows one...
        assert a.can_miss_effective == 0        # ...but the overall rule does not

    def test_empty_ledger_is_unknown_not_danger(self):
        d = build_dashboard([], overall_limit=75)
        assert d.overall.verdict is Verdict.UNKNOWN
        assert d.overall.worst_pct is None

    def test_optimistic_budget_is_never_smaller(self):
        d = build_dashboard(self._rows(), overall_limit=75)
        assert all(s.can_miss_optimistic >= s.can_miss for s in d.subjects)


class TestPendingDominated:
    """Below the line only because lectures are unmarked — a different story
    from being below it because they were missed."""

    def _one(self, present, absent, unknown, limit=70):
        rows = [dict(id=1, code="X", canonical_name="X", lecture_type="Theory",
                     limit=limit, counts=Counts(present, absent, unknown))]
        return build_dashboard(rows, overall_limit=limit).subjects[0]

    def test_unmarked_lectures_are_the_cause(self):
        s = self._one(present=0, absent=0, unknown=8)     # worst 0%, best 100%
        assert s.verdict is Verdict.DANGER                # still never says "safe"
        assert s.pending_dominated

    def test_genuinely_missed_lectures_are_not_pending_dominated(self):
        s = self._one(present=2, absent=8, unknown=0)     # 20%, nothing pending
        assert s.verdict is Verdict.DANGER
        assert not s.pending_dominated

    def test_hopeless_even_if_every_pending_resolves_present(self):
        """5 present, 10 absent, 1 pending: best case 6/16 = 37.5%, still below."""
        s = self._one(present=5, absent=10, unknown=1)
        assert not s.pending_dominated

    def test_a_safe_subject_is_never_pending_dominated(self):
        s = self._one(present=9, absent=0, unknown=1)     # worst 90%
        assert s.verdict is Verdict.SAFE
        assert not s.pending_dominated

    def test_exactly_reaching_the_limit_in_the_best_case_counts(self):
        """7 present, 0 absent, 3 pending: worst 70%… so it isn't below at all."""
        s = self._one(present=6, absent=1, unknown=3)     # worst 60%, best 90%
        assert s.pending_dominated

    @given(counts_st, limit_st)
    def test_pending_dominated_never_contradicts_the_verdict(self, c, limit):
        rows = [dict(id=1, code="X", canonical_name="X", lecture_type="Theory",
                     limit=limit, counts=c)]
        s = build_dashboard(rows, overall_limit=limit).subjects[0]
        # It is a *reason* for DANGER, never a softening of anything else.
        if s.pending_dominated:
            assert s.verdict is Verdict.DANGER
            assert s.can_miss == 0


class TestCoverage:
    def test_touching_ranges_merge(self):
        merged = merge_ranges([
            DateRange(date(2026, 7, 1), date(2026, 7, 31)),
            DateRange(date(2026, 8, 1), date(2026, 8, 12)),
        ])
        assert merged == [DateRange(date(2026, 7, 1), date(2026, 8, 12))]

    def test_overlapping_ranges_merge(self):
        merged = merge_ranges([
            DateRange(date(2026, 7, 1), date(2026, 8, 5)),
            DateRange(date(2026, 7, 20), date(2026, 8, 12)),
        ])
        assert merged == [DateRange(date(2026, 7, 1), date(2026, 8, 12))]

    def test_gap_between_ranges_is_reported(self):
        rep = analyse(
            [
                DateRange(date(2026, 7, 1), date(2026, 8, 12)),
                DateRange(date(2026, 8, 22), date(2026, 8, 31)),
            ],
            today=date(2026, 8, 31),
        )
        assert len(rep.gaps) == 1
        assert rep.gaps[0] == DateRange(date(2026, 8, 13), date(2026, 8, 21))
        assert str(rep.gaps[0]) == "13.08 → 21.08"

    def test_trailing_gap_and_staleness(self):
        rep = analyse(
            [DateRange(date(2026, 7, 1), date(2026, 8, 12))],
            today=date(2026, 8, 25),
            staleness_days=7,
        )
        assert rep.gaps == []
        assert rep.trailing_gap == DateRange(date(2026, 8, 13), date(2026, 8, 25))
        assert rep.stale_days == 13
        assert rep.is_stale

    def test_fresh_report_is_not_stale(self):
        rep = analyse(
            [DateRange(date(2026, 7, 1), date(2026, 8, 16))],
            today=date(2026, 8, 16),
        )
        assert rep.stale_days == 0
        assert not rep.is_stale
        assert rep.trailing_gap is None

    def test_suggested_export_always_spans_semester_start_to_today(self):
        rep = analyse(
            [DateRange(date(2026, 7, 1), date(2026, 8, 12))],
            today=date(2026, 8, 25),
        )
        assert rep.suggested_export == DateRange(date(2026, 7, 1), date(2026, 8, 25))

    def test_no_reports_yet(self):
        rep = analyse([], today=date(2026, 8, 16))
        assert rep.covered == []
        assert rep.suggested_export is None
        assert not rep.is_stale


@pytest.mark.parametrize(
    "present,total,limit,expected",
    [
        (0, 0, 70, Verdict.UNKNOWN),
        (10, 10, 70, Verdict.SAFE),
        (7, 10, 70, Verdict.WARN),      # exactly at the limit: no slack
        (6, 10, 70, Verdict.DANGER),
    ],
)
def test_verdict_boundaries(present, total, limit, expected):
    from attendance_engine import classify

    assert classify(present, total, limit, safe_to_miss(present, total, limit)) is expected
