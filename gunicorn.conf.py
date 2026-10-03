"""gunicorn -c gunicorn.conf.py run:app

Threaded workers: most requests are short reads, and a PDF upload holds a
thread for a second or two rather than a whole process. The timeout bounds
how long a hostile PDF can keep a worker busy.
"""
import multiprocessing
import os
import sys

from dotenv import load_dotenv

# `.env` is read here as well as in config.py, because the settings below are
# needed before the app is imported. Real environment variables still win.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# gunicorn is the production server, so production is what it runs unless told
# otherwise. Left to the app's own default, a missing or misspelt BUNKK_CONFIG
# would quietly serve real users in debug mode with the development secret;
# this way the same mistake stops at boot, naming what is missing.
if not os.environ.get("BUNKK_CONFIG"):
    os.environ["BUNKK_CONFIG"] = "config.ProdConfig"
elif os.environ["BUNKK_CONFIG"] != "config.ProdConfig":
    # Allowed — the load test drives a debug build through gunicorn — but
    # never silently: this is the line to find when a server misbehaves.
    print(f"WARNING: gunicorn is serving {os.environ['BUNKK_CONFIG']}, "
          "not config.ProdConfig. Never do this for real users.", file=sys.stderr)

bind = os.environ.get("BUNKK_BIND", "127.0.0.1:8000")
workers = int(os.environ.get("WEB_CONCURRENCY") or min(4, multiprocessing.cpu_count() * 2 + 1))
worker_class = "gthread"
threads = int(os.environ.get("BUNKK_THREADS") or 4)
timeout = 60
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS") or "127.0.0.1"
# gunicorn's runtime control socket lives in the service user's home directory
# and nothing here uses it; a locked-down service account may not have one.
control_socket_disable = True
