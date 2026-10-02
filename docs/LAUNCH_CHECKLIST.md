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
- [x] Concurrent first uploads claiming one student number answer 422.
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

## Accepted for launch (documented, not changed)

- The "identity claimed" message names the other account's username and a
  masked email. This is deliberate: the reader is almost always the owner.
- `/login/dev` signs in as any address without Google. It answers 404
  unless the app is in debug mode *and* `BUNKK_DEV_LOGIN=1`; production never
  runs in debug mode.

## Operator steps at deploy time

- Generate `SECRET_KEY`; set every variable in `.env.example`.
- Create the Google OAuth client (README, "Google sign-in") with the
  production redirect URI `https://<host>/auth/google/callback`, publish the
  consent screen, and set `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`.
- Point `RATELIMIT_STORAGE_URI` at Redis if running more than one gunicorn
  worker, otherwise limits are per worker.
- Run `flask db upgrade`. This release drops the password columns; existing
  accounts keep everything and are adopted by the first Google sign-in with
  the same address.
- Never copy the development `instance/` folder to the server.
