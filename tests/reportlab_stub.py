"""Tiny PDF writer — enough to build test fixtures without pulling in reportlab.

`make_blank_pdf` writes a non-report PDF (used to test rejection).
`make_detailed_pdf` writes a *valid* detailed attendance report, which lets
tests cover report shapes the three real golden samples don't contain (a fresh
semester, a lecture resolving, a row disappearing) without needing the portal.
"""
from datetime import date, time
from pathlib import Path

LEADING = 14  # points between text lines


def _pdf(path: Path, lines: list[str]) -> None:
    """One page of left-aligned text at 9pt. Enough for pdfplumber to read."""
    parts = ["BT /F1 9 Tf 40 750 Td 14 TL"]
    for i, line in enumerate(lines):
        safe = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        parts.append(f"({safe}) Tj" if i == 0 else f"T* ({safe}) Tj")
    parts.append("ET")
    stream = " ".join(parts).encode("latin-1", "replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
         b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects)+1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF").encode()
    Path(path).write_bytes(bytes(out))


def make_blank_pdf(path: Path, text: str = "hello") -> None:
    _pdf(Path(path), [text])


def _clock(value: time) -> str:
    hour = value.hour % 12 or 12
    meridiem = "AM" if value.hour < 12 else "PM"
    return f"{hour}:{value.minute:02d}:{value.second:02d} {meridiem}"


def make_detailed_pdf(
    path,
    rows,
    *,
    period_start: date = date(2026, 7, 1),
    period_end: date = date(2026, 8, 12),
    student_name: str = "SOHAM NONDA",
    student_number: str = "60000000001",
    roll_no: str = "C000",
    session: str = "2026-2027, Semester III",
    program: str = "B.Tech in Computer Engineering",
) -> None:
    """Write a detailed report. `rows` are (course, date, start, end, status)."""
    lines = [
        "Attendance Report",
        f"Student Name {student_name}",
        f"Student Number {student_number}",
        f"Roll No. {roll_no}",
        f"Academic Year & Academic Session {session}",
        f"Program Name {program}",
        f"Attendance Report Duration : From {period_start:%d.%m.%Y} to {period_end:%d.%m.%Y}",
        "Sr No. Course Name Date Start Time End Time Attendance",
    ]
    for i, (course, on, start, end, status) in enumerate(rows, start=1):
        lines.append(
            f"{i} {course} {on:%b} {on.day}, {on:%Y} "
            f"{_clock(start)} {_clock(end)} {status}"
        )
    lines.append("Legend: P-Present A-Absent NU-Not Updated")
    _pdf(Path(path), lines)
