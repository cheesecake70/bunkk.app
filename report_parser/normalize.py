"""Course-name normalisation and short-code suggestion.

Report course names arrive with glued-on suffixes: a lecture-type letter and
batch/division tokens, e.g.
    "Computer NetworksT C2"            -> Computer Networks           (Theory)
    "Data Structures LaboratoryP C22"  -> Data Structures Laboratory  (Practical)
    "Universal Human Values TutorialU C2" -> Universal Human Values Tutorial (Tutorial)
    "Operations Research T Div- 2"     -> Operations Research         (Theory)
    "Database Management System Lab C22" -> Database Management System Laboratory (Practical)
    "Innovative Product Development I Comp" -> Innovative Product Development I (Unknown)
    "Statistics for Data ScienceT C1-C2" -> Statistics for Data Science (Theory)

Rules are deliberately conservative; anything not confidently normalised keeps
its cleaned name with LectureType.UNKNOWN so the app can ask the user once.
"""
from __future__ import annotations

import re

from .types import LectureType

# Trailing batch/division tokens, tried repeatedly until none match.
_BATCH_TOKEN = re.compile(
    r"""\s*(
          C\d+(-C\d+)?      # C2, C22, C1-C2
        | Div-?\s*\d+       # Div- 2, Div-2, Div 2
        | Comp              # whole-class marker
        )\s*$""",
    re.VERBOSE,
)

# Words that end a name and mean "practical", after suffix stripping.
_LAB_WORDS = ("laboratory", "lab")


def normalize_course(raw: str) -> tuple[str, LectureType]:
    """Return (canonical_name, lecture_type) for a raw printed course name."""
    name = re.sub(r"\s+", " ", raw).strip()

    # 1) strip trailing batch tokens (possibly several)
    while True:
        stripped = _BATCH_TOKEN.sub("", name)
        if stripped == name:
            break
        name = stripped.strip()

    # 2) trailing lecture-type letter glued to the last word or standalone:
    #    "...NetworksT" / "...LaboratoryP" / "...TutorialU" / "Operations Research T"
    ltype = LectureType.UNKNOWN
    m = re.search(r"^(.*?)[ ]?([TPU])$", name)
    if m and (name.endswith((" T", " P", " U")) or re.search(r"[a-z][TPU]$", name)):
        base, letter = m.group(1).rstrip(), m.group(2)
        ltype = {"T": LectureType.THEORY, "P": LectureType.PRACTICAL, "U": LectureType.TUTORIAL}[letter]
        name = base

    # 3) "Lab" spelled short -> "Laboratory" for a stable canonical name
    if name.lower().endswith(" lab"):
        name = name[: -len(" lab")] + " Laboratory"

    # 4) infer type from words when the glued letter was absent
    if ltype is LectureType.UNKNOWN:
        low = name.lower()
        if any(low.endswith(" " + w) or low == w for w in _LAB_WORDS):
            ltype = LectureType.PRACTICAL
        elif low.endswith(" tutorial"):
            ltype = LectureType.TUTORIAL

    # 5) tutorials named "<X> Tutorial" keep the word — it distinguishes the row
    #    from the theory row of the same course.
    return name, ltype


# ---------------------------------------------------------------------------

_STOPWORDS = {"for", "of", "the", "and", "in", "to", "a", "an"}

# Well-known abbreviations produce nicer codes than initials alone.
_KNOWN = {
    "database management system": "DBMS",
    "data structures": "DS",
    "computer networks": "CN",
    "operations research": "OR",
    "universal human values": "UHV",
    "computational mathematics": "Math",
    "statistics for data science": "Stats",
    "innovative product development i": "IPD",
    "community engagement service": "CES",
    "design thinking": "DT",
    "python programming": "Py",
}


def suggest_code(canonical_name: str, lecture_type: LectureType) -> str:
    """Short display code, e.g. 'CN', 'DBMS Lab', 'UHV Tut'. User-editable later."""
    low = canonical_name.lower()

    suffix = ""
    base = low
    if low.endswith(" laboratory"):
        base, suffix = low[: -len(" laboratory")], " Lab"
    elif low.endswith(" tutorial"):
        base, suffix = low[: -len(" tutorial")], " Tut"
    elif lecture_type is LectureType.PRACTICAL:
        suffix = " Lab"

    if base in _KNOWN:
        return _KNOWN[base] + suffix

    words = [w for w in re.split(r"[^a-z0-9]+", base) if w and w not in _STOPWORDS]
    if not words:
        return canonical_name[:8]
    if len(words) == 1:
        return words[0][:5].capitalize() + suffix
    return "".join(w[0].upper() for w in words) + suffix
