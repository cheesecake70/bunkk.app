# Bunkk

Attendance calculator & manager for university students. Upload your college
attendance PDF → know exactly what you can skip.

The design system lives in `docs/design.md`; the pre-launch hardening list in
`docs/LAUNCH_CHECKLIST.md`.

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
- The **GO/SKIP hero**: today's verdict in one word, and leave-early /
  arrive-late options on partial days.
- Plan-ahead simulator: commit future absences (whole day or one subject) and
  watch every budget recompute; over-commitment names what breaks.

**Phase 3** — the walkthrough pass: everything a student hit using it for real.

- A fifth day verdict, **PLANNED**. A day you have already written off is not
  advice, and calling it SKIP told people their own over-spend was safe. It
  carries `over_budget`, so a commitment that broke a limit says so instead of
  staying green — and the day sheet, the month grid and Today's hero all repaint
  from the same payload rather than reloading.
- **Committing warns before and after**: a new absence that would newly break a
  limit asks first, and says what it cost afterwards, with Undo.
- **Bulk guesses.** Fifty unmarked lectures answered in one tap, per subject or
  across the board, with the figures patched in place. One at a time, each with
  a page reload, is not something anyone finished.
- The horizon is drawn as **month grids** rather than one long row of chips, and
  weekends get no column unless the timetable uses them.
- **Phones get a bottom tab bar** (the top nav wrapped into three rows), and the
  timetable shows one day at a time.
- **Sign out every other device** from Settings; sessions carry a random token
  rather than the row id, so a stale cookie can't reach an account that reused it.
- Holidays can be marked a **range** at a time; a whole-day absence on a day with
  no classes is refused rather than silently stored.

**Multi-user** — the schema was multi-tenant from Phase 0, so this is about
everything that only becomes a question with a second person:

- **Google sign-in, and nothing else.** There is no password anywhere: the
  first "Continue with Google" creates the account, with a username suggested
  from the address that you can change in Settings. Google vouches for the
  email, so there is no verification link to click either. Accounts are keyed
  on Google's stable subject id, so renaming your address at Google keeps your
  ledger.
- **One student, one account.** A student number is claimed by the first
  account to upload it and is unique thereafter, so uploading a friend's PDF
  can no longer silently claim their identity and build a second, diverging
  copy of their ledger.
- **SQLite made fit for concurrency** (ADR-2): WAL so readers aren't blocked by
  a writer, a busy timeout so contention waits instead of erroring, and
  foreign keys enforced so a deleted account can't orphan a ledger.
- **Leaving is easy**: type your username to confirm and every row and the
  raw PDFs go.
- Production refuses to boot with the development `SECRET_KEY` — forgeable
  sessions stop being a dev nicety once other people have accounts — or
  without a Google OAuth client, a database URL and the hostnames it answers for.
- **Hardened for a public domain**: CSRF tokens on every form and fetch, a
  per-IP rate limit on starting a sign-in, `Secure` and `SameSite` on both
  cookies, trusted-host checking so a forged `Host` header can't shape the
  OAuth redirect URL, security headers on every response, and JSON errors
  from `/api` (a corrupt PDF is a 422, never a 500).

Worst case drives every number on screen: a pending lecture counts as absent
until the college says otherwise, so a green verdict is always safe. The day
verdict's property test asserts exactly that — a SKIP can never break a limit,
and never describes a day with nothing left to decide.

## Dev setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
flask db upgrade                  # create/upgrade instance/bunkk.db
python -m pytest tests/ -q        # should be all green
python run.py                     # http://127.0.0.1:5000
```

Sign in with Google at `/login` (set up below), or run with
`BUNKK_DEV_LOGIN=1` and use `/login/dev` to sign in as any address without
Google. Then drop a detailed-report PDF on `/upload`.

### Google sign-in

Bunkk signs people in through Google (OpenID Connect via Authlib) and has no
passwords of its own. It needs one OAuth client from Google Cloud Console:

1. <https://console.cloud.google.com/> → create a project (or pick one).
2. **APIs & Services → OAuth consent screen**: External, app name "Bunkk",
   your support email, scopes `openid`, `email`, `profile`. While the app is
   in *Testing* only listed test users can sign in; **Publish** it so any
   Google account can.
3. **APIs & Services → Credentials → Create credentials → OAuth client ID**,
   type *Web application*. Add an **Authorised redirect URI** for every host
   you serve from — the path is always `/auth/google/callback`:
   `http://localhost:5000/auth/google/callback` for dev,
   `https://bunkk.example.com/auth/google/callback` in production.
4. Copy the client ID and secret into `.env` as `GOOGLE_CLIENT_ID` and
   `GOOGLE_CLIENT_SECRET`.

Google only vouches for verified addresses, and Bunkk refuses any claim
without `email_verified`. An account that predates Google sign-in is adopted
by the first Google sign-in with the same address.

## Layout

```
app/                 Flask app (factory, models, auth, pages, JSON API, merge)
  merge.py           snapshot -> LectureLedger fold, diffs, gap flags
  services.py        the only place DB rows become engine inputs (user-scoped)
  planning.py        advanced mode: timetable, calendar, wallet, day verdicts
  auth.py            Google sign-in (Authlib), the only way in
  account.py         profile, sign-out-everywhere, account deletion
  cache.py           per-request memoisation, dropped on write
report_parser/       pure PDF -> typed data (no Flask/DB imports)
attendance_engine/   pure math: budgets, percentages, coverage
templates/           Jinja pages
static/css/          tokens.css + components.css (design system) + app.css
static/js/           html.js fmt.js api.js toast.js commit.js (shared),
                     then one file per page: today, plan, daysheet, calendar,
                     timetable, subject(s), predictions, ladder, upload
tools/make_icons.py  regenerates the favicon and touch icon from the design tokens
tools/loadtest/      seed N students into a scratch DB, then drive them (see file docstrings)
docs/                design system, launch checklist
gunicorn.conf.py     production server settings
tests/golden/        real portal PDFs used as parser ground truth
migrations/          Alembic (SQLite now, Postgres later via ADR-2)
```

## Deploying for more than yourself

```bash
cp .env.example .env            # then fill every value in
python -c "import secrets; print(secrets.token_hex(32))"   # -> SECRET_KEY
flask db upgrade
gunicorn -c gunicorn.conf.py run:app
```

`.env` is loaded automatically. With `BUNKK_CONFIG=config.ProdConfig` the app
refuses to start unless `SECRET_KEY`, `DATABASE_URL`, `GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET` and `BUNKK_TRUSTED_HOSTS` are all set — each of those
fails quietly otherwise. Put nginx or Caddy in front with HTTPS and pass
`X-Forwarded-For` / `-Proto` / `-Host`; the app trusts exactly one proxy hop.
With more than one gunicorn worker, point `RATELIMIT_STORAGE_URI` at Redis so
the sign-in throttle is shared rather than per worker.

**Upgrading past `c7f1a4b82e50` signs everyone out once.** Sessions used to
carry the user's row id, and SQLite hands a deleted row's id to the next account
created — so a leftover cookie could reach a stranger's ledger. They carry a
random token now; the old cookies match nothing and resolve to a login page.

Back up `instance/bunkk.db` *and* `instance/uploads/` together —
the ledger is replayable from the snapshots only if the PDFs survive with it.

Two SQLite notes. Its DDL isn't transactional, so a migration that fails
part-way leaves the half-created table behind while the revision stays at the
old version — drop the stray table before re-running `flask db upgrade`. And in
WAL mode the database is three files: never move or copy `bunkk.db` without
`bunkk.db-wal` beside it, or you silently lose everything not yet
checkpointed.

## Next (the rest of opening up)

More college adapters (the `College` entity and parser interface are the
insurance), then evaluate paid features once retention is proven. Move to
Postgres when one of ADR-2's triggers fires — sustained concurrent writes,
more than one app server, or a public launch — which is a connection-string
change plus a migration run.
