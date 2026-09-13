# Launch checklist

Working list from the pre-launch review. Ticked items are done in code and
covered by a test where one makes sense.

## Must-fix (security)

- [x] CSRF protection on every form and JSON call (Flask-WTF `CSRFProtect`,
      token in a `<meta>` tag, sent as `X-CSRFToken` by `static/js/api.js`).
- [x] Password-reset links can no longer be poisoned through the Host header
      (`TRUSTED_HOSTS` from `BUNKR_TRUSTED_HOSTS`, `ProxyFix`, https scheme).
- [x] A corrupt or non-PDF upload answers 422, never 500.
- [x] Per-IP rate limits on login, registration and forgot-password
      (Flask-Limiter; storage from `RATELIMIT_STORAGE_URI`). Sized for a
      campus NAT: only failed sign-ins count against the login ceiling.
- [x] Remember-me cookie is `Secure`, `HttpOnly`, `SameSite=Lax` in production.
- [x] Production refuses to boot without `SECRET_KEY`, `DATABASE_URL`,
      `MAIL_SERVER`, `MAIL_DEFAULT_SENDER` and `BUNKR_TRUSTED_HOSTS`.
- [x] Golden test PDFs carry a fictional student instead of a real one.
- [x] `gunicorn.conf.py` with threaded workers and a request timeout.

## Should-fix (robustness, cleanliness)

- [x] JSON error bodies from `/api` for 401, 404, 413, 429 and 500.
- [x] Security headers on every response (HSTS in production only).
- [x] Logging wired to gunicorn's handlers in production.
- [x] `run.py` takes its debug flag from the config, not a literal.
- [x] Unknown identifiers cost a password hash check too (no timing tell).
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

## Accepted for launch (documented, not changed)

- Registration does not verify email. Mitigation: a squatted address can be
  reclaimed by its real owner through forgot-password, and registration is
  rate-limited per IP.
- The "identity claimed" message names the other account's username and a
  masked email. This is deliberate: the reader is almost always the owner.
- Account lockout after five failures can be triggered against any account.
  Mitigation: the per-IP limiter makes it expensive to do at scale, and the
  lock is fifteen minutes.

## Operator steps at deploy time

- Generate `SECRET_KEY`; set every variable in `.env.example`.
- Point `RATELIMIT_STORAGE_URI` at Redis if running more than one gunicorn
  worker, otherwise limits are per worker.
- Run `flask db upgrade` (this release signs nobody out, but backfills
  `report_snapshot.semester_id`).
- Never copy the development `instance/` folder to the server.
