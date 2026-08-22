"""Jinja display helpers.

Formatting is the *only* place a percentage becomes a float — every decision
upstream is exact integer arithmetic (§4).
"""
from __future__ import annotations

from datetime import date, time
from fractions import Fraction


def percent(value: Fraction | float | None, places: int = 1) -> str:
    """73.4%  ·  '—' when there is nothing to divide by."""
    if value is None:
        return "—"
    return f"{float(value):.{places}f}%"


def percent_width(value: Fraction | float | None) -> str:
    """Clamped 0–100 width for meter fills."""
    if value is None:
        return "0"
    return f"{max(0.0, min(100.0, float(value))):.2f}"


def short_date(value: date | None) -> str:
    return value.strftime("%d.%m") if value else "—"


def long_date(value: date | None) -> str:
    return value.strftime("%d %b") if value else "—"


def full_date(value: date | None) -> str:
    return value.strftime("%d %b %Y") if value else "—"


def clock(value: time | None) -> str:
    return value.strftime("%H:%M") if value else "—"


STATUS_WORDS = {
    "P": "Present",
    "A": "Absent",
    "AG": "Granted",
    "L": "Late",
    "NU": "Pending",
}

#: Maps a lecture status to the design system's status colour language.
STATUS_TONE = {
    "P": "safe",
    "AG": "safe",
    "A": "danger",
    "L": "pending",
    "NU": "pending",
}


#: One word per day verdict, for every surface. Kept here rather than in the
#: templates because the labels lived in three places — the Today hero, the day
#: strip and plan.js — and had already drifted: the same verdict read "GO" in
#: one and "Must go" in another.
VERDICT_WORDS = {
    "skip": "Skip",
    "partial": "Part skip",
    "go": "Can't skip",
    "off": "No class",
}


def verdict_word(value) -> str:
    """'Can't skip'. Accepts the enum or its value."""
    key = getattr(value, "value", value)
    return VERDICT_WORDS.get(key, str(key))


def status_word(code: str) -> str:
    return STATUS_WORDS.get(code, code)


def status_tone(code: str) -> str:
    return STATUS_TONE.get(code, "neutral")


def register(app) -> None:
    app.jinja_env.filters.update(
        percent=percent,
        percent_width=percent_width,
        short_date=short_date,
        long_date=long_date,
        full_date=full_date,
        clock=clock,
        status_word=status_word,
        status_tone=status_tone,
        verdict_word=verdict_word,
    )
    # The strip repaints itself after a plan changes, so the client needs the
    # same labels the server rendered. Exposed as data rather than duplicated.
    app.jinja_env.globals["VERDICT_WORDS"] = VERDICT_WORDS
