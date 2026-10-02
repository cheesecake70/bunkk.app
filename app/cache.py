"""Per-request memoisation for the read-mostly lookups.

A page like /plan asks the database the same questions six times over — "where
does the horizon end", "what does the ledger say", "what are the limits" —
because the wallet, the day strip and the page shell each derive them
independently rather than passing them around. Threading every value through by
hand would put a parameter on almost every function in `planning`; holding the
answers for the life of one request gets the same result without the plumbing.

Only pure reads belong here. The rule that keeps this safe is narrow and worth
stating: **nothing cached may be written and then read again inside a single
request.** Where that could happen — the timetable, which `save_timetable`
rewrites — the cache is dropped explicitly at the point of the write rather
than left to luck.

Outside a request (tests, a shell, a future CLI) the decorator is a no-op, so
nothing here can make a long-lived process serve stale data.
"""
from __future__ import annotations

from functools import wraps

from flask import g, has_request_context

#: Where the memo lives on Flask's per-request `g`. Named rather than anonymous
#: so `drop` and the tests can find it.
STORE = "_bunkr_request_cache"


def per_request(fn):
    """Memoise `fn` on its arguments for the current request.

    Unhashable arguments fall through uncached rather than raising: a helper
    that grows a list parameter later should get slower, not broken.
    """
    name = f"{fn.__module__}.{fn.__qualname__}"

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not has_request_context():
            return fn(*args, **kwargs)
        try:
            key = (name, args, tuple(sorted(kwargs.items())))
            hash(key)
        except TypeError:
            return fn(*args, **kwargs)
        store = g.setdefault(STORE, {})
        if key not in store:
            store[key] = fn(*args, **kwargs)
        return store[key]

    return wrapper


def drop(*functions) -> None:
    """Forget every memoised result for `functions`.

    Call this straight after writing what they read. It takes the decorated
    functions themselves rather than strings so a rename can't silently leave a
    stale entry behind.
    """
    if not has_request_context():
        return
    store = g.get(STORE)
    if not store:
        return
    names = {f"{fn.__module__}.{fn.__qualname__}" for fn in functions}
    for key in [k for k in store if k[0] in names]:
        del store[key]
