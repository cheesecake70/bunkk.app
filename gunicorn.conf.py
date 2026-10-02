"""gunicorn -c gunicorn.conf.py run:app

Threaded workers: most requests are short reads, and a PDF upload holds a
thread for a second or two rather than a whole process. The timeout bounds
how long a hostile PDF can keep a worker busy.
"""
import multiprocessing
import os

bind = os.environ.get("BUNKR_BIND", "127.0.0.1:8000")
workers = int(os.environ.get("WEB_CONCURRENCY", min(4, multiprocessing.cpu_count() * 2 + 1)))
worker_class = "gthread"
threads = int(os.environ.get("BUNKR_THREADS", "4"))
timeout = 60
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
