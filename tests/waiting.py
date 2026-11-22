"""Generous waits for tests that depend on threads, sockets and timers.

A wait that is right on an idle machine can be too tight on a loaded CI runner or with
`pytest -n 6`. These helpers return the moment the condition holds, so a passing test pays
nothing for the long limit; only a real failure waits it out, and then names what never happened.
They never change what a test asserts, only how long it is willing to wait to see it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

WAIT = 30.0  # seconds: far longer than a loaded machine needs, short enough to fail fast in CI
STEP = 0.05


def wait_until(
    check: Callable[[], Any], what: str, timeout: float = WAIT, step: float = STEP
) -> Any:
    """Poll `check` every `step` seconds until it returns something truthy, and return that."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out after {timeout:g} s waiting for {what}.")
        time.sleep(step)


def wait_event(event: threading.Event, what: str, timeout: float = WAIT) -> None:
    """Block until `event` is set; fail naming `what` if it never is."""
    if not event.wait(timeout):
        raise AssertionError(f"Timed out after {timeout:g} s waiting for {what}.")


def join_thread(thread: threading.Thread, what: str, timeout: float = WAIT) -> None:
    """Join `thread`; fail naming `what` if it is still running afterwards."""
    thread.join(timeout)
    if thread.is_alive():
        raise AssertionError(f"{what} was still running after {timeout:g} s.")
