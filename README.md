# Bunkmate 🎒

Attendance calculator & manager for university students. Upload your college
attendance PDF → know exactly what you can skip.

Full docs live in the Claude project: `prd.md`, `implementation-plan.md`,
`design.md`.

## Status: Phase 3 ✅ — installable PWA with a morning notification

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
  upload with live diff, snapshot history, settings with per-subject overrides.
- Auth from day 1; every row keyed by `user_id`, with tests that one account
  can never read another's ledger.

**Phase 2** — planning on exact numbers instead of estimates:

- Timetable **inferred** from the ledger (recurring subject/weekday/time
  patterns with a seen-in-N-weeks confidence signal) and confirmed with
  checkboxes — never typed in. Saved as effective-dated versions.
- Semester calendar: one date picker for the end date, then tap any future day
  to cycle normal → holiday → swap-day. The past is read-only by design.
- The **bunk wallet**: exact remaining lectures per subject, budgets that
  planned absences spend first, and lectures held since the last upload counted
  as unknowns (they can't be attended any more, so ignoring them would
  overstate the budget).
- The **GO/SKIP hero**: today's verdict in one word, a two-week day strip,
  leave-early / arrive-late options on partial days, and "breaks first" naming
  the tightest subject.
- Plan-ahead simulator: commit future absences (whole day or one subject) and
  watch every budget recompute; over-commitment names what breaks.

**Phase 3** — the habit layer, fully passive (no manual entry, ever):

- **Installable PWA**: manifest, generated icon set, and a service worker served
  from `/` so its scope covers the app. It caches **static assets only** —
  pages are never stored, because a cached page is one student's attendance
  sitting on a device, and stale numbers are exactly how a "safe to bunk"
  verdict goes wrong. Offline shows a shell that says so.
- **Morning notification**: one push a day with tomorrow's verdict
  ("Tomorrow: skippable — 2× DBMS Lab, Math… all within budget").
- **The nudge carries its own incentive**: every brief ends with "upload a
  fresh report to unlock up to N more" — computed by re-running the wallet with
  every unresolved lecture assumed present. It's explicitly the best case and
  never authorises a bunk.
- Push is **optional infrastructure**: with no VAPID keys the app is fully
  usable and Settings explains why notifications are unavailable, so dev and
  production don't diverge.

Worst case drives every number on screen: a pending lecture counts as absent
until the college says otherwise, so a green verdict is always safe. The
day-strip property test asserts exactly that: a SKIP verdict can never break
a limit.

## Dev setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
flask db upgrade                  # create/upgrade instance/bunkmate.db
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
  push.py            Web Push delivery + the morning-brief fan-out
  cli.py             cron surface: flask push-briefs / flask vapid-keys
report_parser/       pure PDF -> typed data (no Flask/DB imports)
attendance_engine/   pure math: budgets, percentages, coverage
templates/           Jinja pages
static/css/          tokens.css + components.css (design system) + app.css
static/js/           sw.js (service worker), pwa.js, upload.js, calendar.js
tools/make_icons.py  regenerates the PWA icon set from the design tokens
tests/golden/        real portal PDFs used as parser ground truth
migrations/          Alembic (SQLite now, Postgres later via ADR-2)
```

## Notifications (optional)

```bash
flask vapid-keys          # prints the three env vars, generate once
```

Put them in the environment, restart, and the toggle in Settings comes alive.
Then one hourly cron line covers every user, since each run only picks up the
people whose chosen hour matches:

```bash
0 * * * * cd /srv/bunkmate && .venv/bin/flask push-briefs >> /var/log/bunkmate.log 2>&1
```

`flask push-briefs --dry-run` prints what would be sent without sending it.

On iPhone, notifications only work once the app is added to the home screen —
iOS restricts push to installed web apps.

## Next (Phase 4 — open it up)

Onboarding and profiles, more college adapters, invites, then evaluate paid
features once retention is proven. The schema has been multi-tenant since
Phase 0; the SQLite→Postgres switch is a connection string plus a migration
run when one of ADR-2's triggers fires.
