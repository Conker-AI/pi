"""One content-free routing result for one uninterrupted foreground turn attempt."""

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from threading import get_ident


class Memo:
    def __init__(self, store_path, turn_id):
        self.store_path, self.turn_id = store_path, turn_id
        self.thread = get_ident()
        self.active = True
        self.key = self.value = None

    def matches(self, store_path, turn_id):
        return (
            self.active
            and self.thread == get_ident()
            and self.store_path == store_path
            and self.turn_id == turn_id
        )

    def get(self, key):
        if self.active and self.thread == get_ident() and key == self.key:
            return deepcopy(self.value)
        return None

    def put(self, key, selected, attempt):
        if self.active and self.thread == get_ident():
            # Only opaque identity/digests and validated model-selection metadata;
            # no request text or completion content is kept here.
            self.key, self.value = key, (selected, deepcopy(attempt))

    def clear(self):
        self.active = False
        self.key = self.value = None


_current = ContextVar("routing_memo", default=None)


@contextmanager
def scope(store_path, turn_id):
    memo = Memo(store_path, turn_id)
    previous = _current.set(memo)
    try:
        yield memo
    finally:
        memo.clear()
        _current.reset(previous)


def current(store_path, turn_id):
    memo = _current.get()
    return memo if memo is not None and memo.matches(store_path, turn_id) else None
