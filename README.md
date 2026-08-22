# Bunkr

Attendance calculator & manager for university students. Upload your college
attendance PDF → know exactly what you can skip.

Full docs live in the Claude project: `prd.md`, `implementation-plan.md`,
`design.md`.

## Status: multi-user — invite your friends onto your own server

**Phase 0** — Flask scaffold (app factory, SQLAlchemy models, Flask-Login,
`/healthz`); `report_parser/` parsing the portal's **detailed** report with
pdfplumber (header, per-lecture rows P/A/AG/L/NU, course-name normalisation,
short display codes, format detection, validation); golden-file tests against
three real portal PDFs, including the full July cross-check against the
portal's own summary report for all 14 subjects.

**Phase 1** — upload → parse → merge → dashboard:

- `attendance_engine/` — pure budget math. Official / worst-case / best-case
  percentages, safe-to-miss counts, recovery counts, coverage gaps and
  staleness. Every threshold is exact integer arithmetic (`100·P ≥ limit·T`);
  property tests prove the budget is exactly maximal and the recovery count
  exactly minimal, so rounding can never flip a verdict.
- `app/merge.py` — snapshot → ledger upsert. Status changes are logged, new
  lectures inserted, identical files short-circuit to "nothing new", rows that
  vanish from the portal are flagged rather than deleted, and an unfamiliar
  course name is asked about once and remembered forever.
- Pages — dashboard (bunk budget, per-subject table with meters, gap /
  staleness / pending banners), per-lecture drill-down with change history,
  upload with live diff and coverage, settings with per-subject overrides.
- Auth from day 1; every row keyed by `user_id`, with tests that one account
  can never read another's ledger.

**Phase 2** — planning on exact numbers instead of estimates:

- Timetable **inferred** from the ledger (recurring subject/weekday/time
  patterns with a seen-in-N-weeks confidence signal) and confirmed with
  checkboxes — never typed in. Saved as effective-dated versions.
- Semester calendar: one date picker for the end date, then tap any future day
  to mark it a holiday. The past is read-only by design.
- The **bunk wallet**: exact remaining lectures per subject, budgets that
  planned absences spend first, and lectures held since the last upload counted
  as unknowns (they can't be attended any more, so ignoring them would
  overstate the budget).
- The **GO/SKIP hero**: today's verdict in one word, a two-week day strip, and
  leave-early / arrive-late options on partial days.
- Plan-ahead simulator: commit future absences (whole day or one subject) and
  watch every budget recompute; over-commitment names what breaks.

**Multi-user** — the schema was multi-tenant from Phase 0, so this is about
everything that only becomes a question with a second person:

- **Open registration**: email, username and a password typed twice. You can
  sign in with either the username or the email.
- **One student, one account.** A student number is claimed by the first
  account to upload it and is unique thereafter, so uploading a friend's PDF
  can no longer silently claim their identity and build a second, diverging
  copy of their ledger.
- **SQLite made fit for concurrency** (ADR-2): WAL so readers aren't blocked by
  a writer, a busy timeout so contention waits instead of erroring, and
  foreign keys enforced so a deleted account can't orphan a ledger.
- **Login lockout** after repeated failures, with identical wording for unknown
  emails and wrong passwords so the form can't enumerate who has an account.
- **Leaving is easy**: a password-confirmed deletion that removes every row and
  the raw PDFs.
- Production refuses to boot with the development `SECRET_KEY` — forgeable
  sessions stop being a dev nicety once other people have accounts.

Worst case drives every number on screen: a pending lecture counts as absent
until the college says otherwise, so a green verdict is always safe. The
day-strip property test asserts exactly that: a SKIP verdict can never break
a limit.

## Dev setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
flask db upgrade                  # create/upgrade instance/bunkr.db
python -m pytest tests/ -q        # should be all green
python run.py                     # http://127.0.0.1:5000
```

Register at `/register`, then drop a detailed-report PDF on `/upload`.

## Layout

```
app/                 Flask app (factory, models, auth, pages, JSON API, merge)
  merge.py           snapshot -> LectureLedger fold, diffs, gap flags
  services.py        the only place DB rows become engine inputs (user-scoped)
  planning.py        advanced mode: timetable, calendar, wallet, day strip
  account.py         profile, data export, account deletion
report_parser/       pure PDF -> typed data (no Flask/DB imports)
attendance_engine/   pure math: budgets, percentages, coverage
templates/           Jinja pages
static/css/          tokens.css + components.css (design system) + app.css
static/js/           upload.js, calendar.js, plan.js
tools/make_icons.py  regenerates the icon set from the design tokens
tests/golden/        real portal PDFs used as parser ground truth
migrations/          Alembic (SQLite now, Postgres later via ADR-2)
```

## Deploying for more than yourself

```bash
export SECRET_KEY=$(python -c "import secrets; print(secrets.token_hex(32))")
export BUNKR_CONFIG=config.ProdConfig
flask db upgrade
gunicorn "run:app"
```

Back up `instance/bunkr.db` *and* `instance/uploads/` together —
the ledger is replayable from the snapshots only if the PDFs survive with it.

Two SQLite notes. Its DDL isn't transactional, so a migration that fails
part-way leaves the half-created table behind while the revision stays at the
old version — drop the stray table before re-running `flask db upgrade`. And in
WAL mode the database is three files: never move or copy `bunkr.db` without
`bunkr.db-wal` beside it, or you silently lose everything not yet
checkpointed.

## Next (Phase 4 — the rest of opening up)

More college adapters (the `College` entity and parser interface are the
insurance), then evaluate paid features once retention is proven. Move to
Postgres when one of ADR-2's triggers fires — sustained concurrent writes,
more than one app server, or a public launch — which is a connection-string
change plus a migration run.
