# Bunkmate 🎒

Attendance calculator & manager for university students. Upload your college
attendance PDF → know exactly what you can skip.

Full docs live in the Claude project: `prd.md`, `implementation-plan.md`,
`onboarding-flow.md`.

## Status: Phase 0 ✅

- Flask scaffold (app factory, SQLAlchemy models, Flask-Login wiring, /healthz)
- `report_parser/` — pure package parsing the portal's **detailed** attendance
  report with pdfplumber: header, per-lecture rows (P/A/AG/L/NU), course-name
  normalisation ("Computer NetworksT C2" → Computer Networks · Theory), short
  display codes (CN, DBMS Lab…), format detection (summary PDFs politely
  redirected), consistency validation
- Golden-file tests against three real portal PDFs, including a full
  cross-check: detailed-report July aggregates == portal's July summary for
  all 14 subjects

## Dev setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest tests/ -v     # should be all green
python run.py                  # http://127.0.0.1:5000/healthz
```

## Layout

```
app/                 Flask app (factory, models, routes)
report_parser/       pure PDF -> typed data (no Flask/DB imports)
attendance_engine/   pure math: budgets & verdicts (Phase 1+)
tests/golden/        real portal PDFs used as parser ground truth
config.py            Dev/Test/Prod configs (SQLite; Postgres-ready)
```

## Next (Phase 1)

Merge engine (per-lecture upsert ledger + diffs + coverage tracking), upload
endpoint, subject-code onboarding, basic dashboard.
