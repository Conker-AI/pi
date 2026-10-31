"""One caller at a time per key, so a retried request waits for the first instead of repeating it."""

import threading
from contextlib import contextmanager

_guard = threading.Lock()
_held = {}


@contextmanager
def per_key(key):
    """Hold the key's lock; the entry is dropped when no caller waits on it."""
    with _guard:
        entry = _held.setdefault(key, [threading.Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _guard:
            entry[1] -= 1
            if not entry[1]:
                del _held[key]
