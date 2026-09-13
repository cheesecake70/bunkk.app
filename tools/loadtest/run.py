"""Threaded load client: N students log in through the real form and use the
app the way students do. Prints latency percentiles and every error class.

usage: python tools/loadtest/run.py [users] [seconds] [host:port] [burst]

`burst` makes every student upload a report at the same instant instead of
browsing — the worst case for SQLite's single writer. Seed the target first
with seed.py; the students are student<N>@example.com, signed in through
/login/dev (run the server with BUNKR_DEV_LOGIN=1 in debug mode). The
client ignores the Secure cookie flag so it can drive a production-shaped
server over plain http on localhost.
"""
import http.client, json, random, re, sys, threading, time, uuid
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode

USERS = int(sys.argv[1]) if len(sys.argv) > 1 else 200
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 60
HOST, PORT = (sys.argv[3] if len(sys.argv) > 3 else "127.0.0.1:8001").split(":")
BURST = len(sys.argv) > 4 and sys.argv[4] == "burst"
import os
PDFS = Path(os.environ.get("BUNKR_LOAD_PDFS", Path(__file__).resolve().parents[2] / "instance" / "load_pdfs"))

stats = defaultdict(list)          # action -> [latency_ms]
errors = defaultdict(list)         # action -> [(status, snippet)]
lock = threading.Lock()
stop_at = time.time() + SECONDS + 15


class Student:
    def __init__(self, i):
        self.i = i
        self.cookies = {}
        self.csrf = ""
        self.conn = http.client.HTTPConnection(HOST, int(PORT), timeout=60)
        self.subject_ids = []

    # --- plumbing -----------------------------------------------------------
    def request(self, action, method, path, body=None, headers=None, json_body=None):
        h = {"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())}
        if json_body is not None:
            body = json.dumps(json_body); h["Content-Type"] = "application/json"
        if method != "GET":
            h["X-CSRFToken"] = self.csrf
        h.update(headers or {})
        t0 = time.perf_counter()
        try:
            self.conn.request(method, path, body=body, headers=h)
            resp = self.conn.getresponse()
            data = resp.read()
        except Exception as exc:                      # connection reset, timeout
            self.conn.close(); self.conn = http.client.HTTPConnection(HOST, int(PORT), timeout=60)
            with lock:
                errors[action].append(("EXC", repr(exc)[:120]))
                stats[action].append((time.perf_counter() - t0) * 1000)
            return None, b""
        ms = (time.perf_counter() - t0) * 1000
        for sc in resp.headers.get_all("Set-Cookie") or []:
            name, _, rest = sc.partition("=")
            self.cookies[name.strip()] = rest.split(";")[0]
        with lock:
            stats[action].append(ms)
            if resp.status >= 400:
                errors[action].append((resp.status, data[:120].decode(errors="replace")))
        return resp, data

    def form(self, action, path, fields):
        fields = dict(fields, csrf_token=self.csrf)
        return self.request(action, "POST", path, body=urlencode(fields),
                            headers={"Content-Type": "application/x-www-form-urlencoded"})

    # --- scenario -----------------------------------------------------------
    def login(self):
        resp, page = self.request("GET /login/dev", "GET", "/login/dev")
        if not resp: return False
        m = re.search(rb'name="csrf-token" content="([^"]+)"', page)
        self.csrf = m.group(1).decode() if m else ""
        resp, _ = self.form("POST /login/dev", "/login/dev",
                            {"email": f"student{self.i}@example.com"})
        return bool(resp) and resp.status == 302

    def upload(self):
        pdf = (PDFS / f"student{self.i}.pdf").read_bytes() + f"\n%{uuid.uuid4().hex}\n".encode()
        boundary = "----bunkr" + uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"report\"; "
                f"filename=\"report.pdf\"\r\nContent-Type: application/pdf\r\n\r\n").encode() \
               + pdf + f"\r\n--{boundary}--\r\n".encode()
        self.request("POST /api/reports (parse+merge)", "POST", "/api/reports", body=body,
                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

    def run(self):
        if not self.login():
            return
        resp, page = self.request("GET /plan", "GET", "/plan")
        if resp:
            self.subject_ids = [int(x) for x in re.findall(rb'data-subject-row="(\d+)"', page)]
            m = re.search(rb'name="csrf-token" content="([^"]+)"', page)
            if m: self.csrf = m.group(1).decode()
        rng = random.Random(self.i)
        uploaded = False
        if BURST:
            self.upload()
            self.request("GET /plan", "GET", "/plan")
            self.conn.close()
            return
        while time.time() < stop_at - 15:
            roll = rng.random()
            if roll < 0.30:
                self.request("GET / (today)", "GET", "/")
            elif roll < 0.50:
                self.request("GET /plan", "GET", "/plan")
            elif roll < 0.65:
                self.request("GET /api/dashboard", "GET", "/api/dashboard")
            elif roll < 0.75:
                day = f"2026-{rng.choice([9,10,11]):02d}-{rng.randint(14,28):02d}"
                self.request("GET /api/day/<date>", "GET", f"/api/day/{day}")
            elif roll < 0.85 and self.subject_ids:
                sid = rng.choice(self.subject_ids)
                self.request("GET /subjects/<id>", "GET", f"/subjects/{sid}")
            elif roll < 0.92:
                self.request("POST /api/simulate", "POST", "/api/simulate",
                             json_body={"absences": [{"date": "2026-09-15"}], "light": True})
            elif roll < 0.97:
                # A weekday: the app rightly refuses a whole-day absence on a Saturday.
                day = f"2026-10-{rng.choice([5,6,7,8,9,12,13,14,15,16,19,20,21,22,23]):02d}"
                resp, data = self.request("POST /api/absences", "POST", "/api/absences",
                                          json_body={"date": day})
                if resp and resp.status == 200:
                    aid = json.loads(data).get("absence_id")
                    self.request("DELETE /api/absences/<id>", "DELETE", f"/api/absences/{aid}")
            elif not uploaded:
                uploaded = True
                self.upload()
            time.sleep(rng.uniform(0.2, 1.5))     # think time
        self.conn.close()


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))] if values else 0


t_start = time.time()
threads = []
for i in range(USERS):
    t = threading.Thread(target=Student(i).run, daemon=True); threads.append(t); t.start()
    time.sleep(2.0 / USERS)                       # everyone arrives within ~2 s
for t in threads:
    t.join(timeout=SECONDS + 60)
elapsed = time.time() - t_start

total = sum(len(v) for v in stats.values())
print(f"\n{USERS} students · {elapsed:.0f}s · {total} requests · {total/elapsed:.1f} req/s\n")
print(f"{'action':38} {'n':>5} {'err':>4} {'p50':>7} {'p95':>7} {'p99':>7} {'max':>7}")
for action in sorted(stats, key=lambda a: -pct(stats[a], 0.95)):
    v = stats[action]
    print(f"{action:38} {len(v):5d} {len(errors[action]):4d} {pct(v,.5):7.0f} {pct(v,.95):7.0f} {pct(v,.99):7.0f} {max(v):7.0f}")
print()
for action, errs in errors.items():
    kinds = defaultdict(int)
    for status, snippet in errs: kinds[(status, snippet[:70])] += 1
    for (status, snippet), n in sorted(kinds.items(), key=lambda kv: -kv[1])[:5]:
        print(f"  {action}: {n}× {status} {snippet!r}")
if not any(errors.values()):
    print("no errors")
