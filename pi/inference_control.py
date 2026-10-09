"""Request-scoped cancellation and foreground priority for shared local inference.

Closing a client stops local consumption, not a promise about remote billing.
Provider adapters may register their own exact-request cancellation operation.
"""

from __future__ import annotations

import contextlib
import contextvars
import socket
import threading
import time
import uuid


class InferenceCancelled(RuntimeError):
    pass


class Token:
    def __init__(self):
        self.request_id = "inference_" + uuid.uuid4().hex
        self.stopped = threading.Event()
        self.reason = None
        self._lock = threading.Lock()
        self._callbacks = set()

    def cancel(self, reason="owner"):
        with self._lock:
            if self.stopped.is_set():
                return
            self.reason = reason
            self.stopped.set()
            callbacks = list(self._callbacks)
        for callback in callbacks:
            # Transport exceptions may contain prompts or credentials.
            with contextlib.suppress(Exception):
                callback()

    def check(self):
        if self.stopped.is_set():
            raise InferenceCancelled("This inference was stopped.")

    @contextlib.contextmanager
    def register(self, callback):
        with self._lock:
            stopped = self.stopped.is_set()
            if not stopped:
                self._callbacks.add(callback)
        if stopped:
            try:
                callback()
            finally:
                self.check()
        try:
            self.check()
            yield
        finally:
            with self._lock:
                self._callbacks.discard(callback)


_token = contextvars.ContextVar("inference_token", default=None)
_background = contextvars.ContextVar("inference_background", default=False)
_condition = threading.Condition()
_local = {}
_foreground_intents = {}
_foreground_waiters = {}


def current():
    return _token.get()


def check():
    token = current()
    if token is not None:
        token.check()


def close_response(response):
    """Interrupt a pending socket read before disposing this response only."""
    stream = getattr(response, "extensions", {}).get("network_stream")
    try:
        connection = stream.get_extra_info("socket") if stream else None
        if connection is not None:
            connection.shutdown(socket.SHUT_RDWR)
    except (AttributeError, OSError, ValueError):
        pass
    response.close()


@contextlib.contextmanager
def bind(token, *, background=False):
    saved = _token.set(token)
    lane = _background.set(background)
    try:
        check()
        yield
    finally:
        _background.reset(lane)
        _token.reset(saved)


@contextlib.contextmanager
def foreground_intent(keys):
    """Reserve the first reply before its newly requested thinker can compete."""
    keys = set(key for key in keys if key)
    with _condition:
        for key in keys:
            _foreground_intents[key] = _foreground_intents.get(key, 0) + 1
    try:
        yield
    finally:
        with _condition:
            for key in keys:
                _foreground_intents[key] -= 1
                if not _foreground_intents[key]:
                    del _foreground_intents[key]
            _condition.notify_all()


@contextlib.contextmanager
def local_capacity(key, timeout):
    """One local request per endpoint; a reply can interrupt a background call.

    A slot stays held until the actual adapter exits. Cancellation never invents
    released compute capacity while an earlier request is still consuming it.
    """
    token = current() or Token()
    background = _background.get()
    deadline = time.monotonic() + timeout
    acquired = False
    with _condition:
        if not background:
            _foreground_waiters[key] = _foreground_waiters.get(key, 0) + 1
    try:
        while not acquired:
            token.check()
            if time.monotonic() >= deadline:
                from .providers import ProviderUnavailable

                raise ProviderUnavailable("Local inference capacity is busy.")
            victim = None
            with _condition:
                active = _local.get(key)
                priority = _foreground_intents.get(key, 0) or _foreground_waiters.get(key, 0)
                if active is None and (not background or not priority):
                    _local[key] = (token, background)
                    acquired = True
                elif not background and active and active[1]:
                    victim = active[0]
                if not acquired and victim is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        from .providers import ProviderUnavailable

                        raise ProviderUnavailable("Local inference capacity is busy.")
                    _condition.wait(min(remaining, 0.1))
            if victim is not None:
                victim.cancel("preempted")
                with _condition:
                    _condition.wait(0.01)
        token.check()
        yield
    finally:
        with _condition:
            if acquired:
                _local.pop(key, None)
            if not background:
                _foreground_waiters[key] -= 1
                if not _foreground_waiters[key]:
                    del _foreground_waiters[key]
            _condition.notify_all()
