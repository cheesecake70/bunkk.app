# Launch checklist

Working list from the pre-launch review. Ticked items are done in code and
covered by a test where one makes sense.

## Must-fix (security)

- [x] CSRF protection on every form and JSON call (Flask-WTF `CSRFProtect`,
      token in a `<meta>` tag, sent as `X-CSRFToken` by `static/js/api.js`).
- [x] The OAuth redirect URL can no longer be poisoned through the Host header
      (`TRUSTED_HOSTS` from `BUNKK_TRUSTED_HOSTS`, `ProxyFix`, https scheme).
- [x] A corrupt or non-PDF upload answers 422, never 500.
- [x] Per-IP rate limit on starting a Google sign-in (Flask-Limiter; storage
      from `RATELIMIT_STORAGE_URI`). Finishing one is never throttled, so a
      campus NAT can't lock a hostel out.
- [x] Remember-me cookie is `Secure`, `HttpOnly`, `SameSite=Lax` in production.
- [x] Production refuses to boot without `SECRET_KEY`, `DATABASE_URL`,
      `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `BUNKK_TRUSTED_HOSTS`.
- [x] Golden test PDFs carry a fictional student instead of a real one.
- [x] No passwords at all: sign-in is Google (OpenID Connect) only, and a
      claim without `email_verified` is refused, so nobody can squat on
      someone else's email and there is nothing to phish, reset or leak.
- [x] `gunicorn.conf.py` with threaded workers and a request timeout.

## Should-fix (robustness, cleanliness)

- [x] JSON error bodies from `/api` for 401, 404, 413, 429 and 500.
- [x] Security headers on every response (HSTS in production only).
- [x] Logging wired to gunicorn's handlers in production.
- [x] `run.py` takes its debug flag from the config, not a literal.
- [x] One account uploading twice at once answers 422, not 500.
- [x] Dashboard, wallet and coverage are scoped to the active semester;
      a new semester's report deactivates the old one.
- [x] Dead code and unused columns removed (`snapshots_for`,
      `Settings.advanced_mode`, `Semester.start_date`, unused CSS).
- [x] Indexes on `subject.semester_id`, `timetable_version.semester_id`,
      `report_snapshot.semester_id`.
- [x] `requirements.txt` pinned and matching what the app imports.
- [x] `.env` loaded automatically (python-dotenv); `.env.example` complete.
- [x] Client error text escaped before it reaches `innerHTML`.
- [x] README deploy section rewritten; docs moved to `docs/`.

## Load test (tools/loadtest)

Run on an 8-core laptop shared with the load generator, gunicorn with the
shipped config (4 workers × 4 threads), ProdConfig, CSRF and limits on:

- 200 students arriving within 2 s and browsing for 60 s: ~11,000 requests,
  150–185 req/s, zero errors, p95 under 2 s for every page.
- 200 students uploading a report at the same instant (worst case for
  SQLite's single writer): zero errors after raising `busy_timeout` to 30 s;
  the slowest upload waited 13 s. Before the fix, 8 of 200 failed with
  "database is locked".
- A lost write lock now answers 503 with `Retry-After` rather than 500.

Move to Postgres when uploads routinely queue for more than a few seconds,
which is the trigger ADR-2 already names.

## Launch pass (tests/test_launch.py)

- [x] No JSON endpoint answers 500 to a malformed body: wrong types, bare
      lists and strings, oversized ints and out-of-range dates are all 4xx.
      Lists a request may carry have ceilings; free text is trimmed to its
      column.
- [x] A semester end or checkpoint more than a year out is refused. A mistyped
      year used to take `/`, `/plan` and `/calendar` down for that account.
- [x] `SECRET_KEY` placeholders (including the one `.env.example` used to
      ship) and keys under 32 characters are refused in production.
- [x] gunicorn runs `ProdConfig` unless `BUNKK_CONFIG` says otherwise, so a
      missing or misspelt variable stops at boot instead of serving in debug.
- [x] A relative `DATABASE_URL` / `BUNKK_UPLOAD_DIR` resolves against the
      repository, not `instance/instance/` or the working directory.
- [x] Calls to Google carry a 10 s timeout; Google being unreachable is a
      sentence on the sign-in page, not a 500.
- [x] An address already owned by one Google identity is never adopted by
      another; two simultaneous first sign-ins make one account.
- [x] Re-uploading a report abandoned at the "same subject?" question asks
      again instead of answering "nothing new".
- [x] A failed merge removes the PDF it had already written.
- [x] Uploads are rate limited per account (20 a minute).
- [x] `/healthz` checks the database.
- [x] The same request twice at once (a double tap, a second tab) gets the
      same answer twice, not a 500: holidays, absences and guesses retry on a
      unique-index clash, and anything else answers 409.
- [x] Whole-day and subject-wide absences are unique in the database, not just
      in the check before the insert (migration `a9c4e17b3d52`, which also
      collapses any duplicates already stored).
- [x] An id in a URL too large for the database is a 404.
- [x] "Today" is worked out in `BUNKK_TIMEZONE` (default Asia/Kolkata), so a
      server on UTC doesn't answer about yesterday until 05:30.
- [x] Pages and API answers are `Cache-Control: no-store`; signing out on a
      shared laptop leaves nothing behind the Back button.
- [x] Migrations and models agree (`flask db check`, run as a test).
- [x] `pip-audit` clean on `requirements.txt`.

## Accepted for launch (documented, not changed)

- An email address already linked to one Google identity is refused for any
  other (a reassigned college mailbox). The new holder gets in once the old
  holder signs in again — their account follows them to their new address —
  or once that account is deleted. There is no self-service route beyond that.
- Migration `a9c4e17b3d52` keeps the oldest of any duplicate planned absences;
  a note on a removed duplicate is lost with it.
- Downgrading below `f49e695321c6` (pre multi-user) fails on an unnamed
  constraint. Every later migration downgrades cleanly.

- A student number is not tied to one account: anyone holding a student's
  report PDF can upload it into an account of their own and see that
  attendance. Accounts never see each other's data.
- `/login/dev` signs in as any address without Google. It answers 404
  unless the app is in debug mode *and* `BUNKK_DEV_LOGIN=1`; production never
  runs in debug mode.

## Operator steps at deploy time

- Generate `SECRET_KEY`; set every variable in the "Required" block of
  `.env.example`. Check the names: a misspelt variable is an unset one.
- Create the Google OAuth client (README, "Google sign-in") with the
  production redirect URI `https://<host>/auth/google/callback`, publish the
  consent screen, and set `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.
- Point `RATELIMIT_STORAGE_URI` at Redis if running more than one gunicorn
  worker, otherwise limits are per worker.
- Run `flask db upgrade`. This release drops the password columns; existing
  accounts keep everything and are adopted by the first Google sign-in with
  the same address.
- Never copy the development `instance/` folder to the server.
