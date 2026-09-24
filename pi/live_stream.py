"""Live reply previews for turns in progress.

A turn's saved message is the answer; this module only lets the owner watch it
being written. Text reaches it from the answer model while the turn runs and is
held in memory, keyed by the caller's request id, for a short while after the
turn ends. Nothing here is persisted or trusted: a client replaces the preview
with the saved message once the turn finishes.

Tool requests are one JSON line (see `tools.py`), so any line that starts with
`{` is held back until it is complete and dropped if it is a tool request. The
owner sees prose, never protocol.
"""

from __future__ import annotations

import contextlib
import contextvars
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

MAX_PREVIEW_CHARACTERS = 200_000
RETAIN_SECONDS = 120.0
_TOOL_LINE = re.compile(r'^\s*\{\s*"tool"\s*:')


@dataclass
class _Reply:
    events: list[dict] = field(default_factory=list)
    finished_at: float | None = None
    shown: int = 0
    pending: str = ""


_replies: dict[str, _Reply] = {}
_lock = threading.Lock()
# The request whose reply may be previewed, and whether an answer call is running.
_request: contextvars.ContextVar[str | None] = contextvars.ContextVar("live_request", default=None)
_active: contextvars.ContextVar[str | None] = contextvars.ContextVar("live_stream", default=None)


def _prune(now: float) -> None:
    for key in [
        key
        for key, reply in _replies.items()
        if reply.finished_at is not None and now - reply.finished_at > RETAIN_SECONDS
    ]:
        del _replies[key]


def _append(reply: _Reply, event: dict) -> None:
    event["seq"] = len(reply.events) + 1
    reply.events.append(event)


@contextlib.contextmanager
def open_reply(request_id: str | None) -> Iterator[None]:
    """Collect previews for this request while the block runs."""
    if not request_id:
        yield
        return
    with _lock:
        _prune(time.monotonic())
        _replies[request_id] = _Reply()
    token = _request.set(request_id)
    try:
        yield
    finally:
        _request.reset(token)
        with _lock:
            reply = _replies.get(request_id)
            if reply is not None:
                _append(reply, {"type": "done"})
                reply.finished_at = time.monotonic()


@contextlib.contextmanager
def answering() -> Iterator[None]:
    """Stream only the owner-facing answer; helper calls stay invisible by default."""
    token = _active.set(_request.get())
    try:
        yield
    finally:
        _active.reset(token)


def active() -> bool:
    return _active.get() is not None


def begin_attempt() -> None:
    """A new model attempt replaces whatever an earlier one had shown."""
    request_id = _active.get()
    if request_id is None:
        return
    with _lock:
        reply = _replies.get(request_id)
        if reply is not None:
            reply.shown = 0
            reply.pending = ""
            _append(reply, {"type": "reset"})


def _visible(reply: _Reply, text: str) -> str:
    """Return prose that is safe to show; hold an unfinished `{` line back."""
    reply.pending += text
    out = []
    while "\n" in reply.pending:
        line, reply.pending = reply.pending.split("\n", 1)
        if not _TOOL_LINE.match(line):
            out.append(line + "\n")
    if reply.pending and not reply.pending.lstrip().startswith("{"):
        out.append(reply.pending)
        reply.pending = ""
    return "".join(out)


def delta(text: str) -> None:
    """Forward a piece of the answer. Silently bounded; never raises into a turn."""
    request_id = _active.get()
    if request_id is None or not text:
        return
    with _lock:
        reply = _replies.get(request_id)
        if reply is None:
            return
        visible = _visible(reply, text)
        room = MAX_PREVIEW_CHARACTERS - reply.shown
        if visible and room > 0:
            visible = visible[:room]
            reply.shown += len(visible)
            _append(reply, {"type": "delta", "text": visible})


def read(request_id: str, after: int = 0) -> tuple[list[dict], bool] | None:
    """Events after `after`, and whether the reply is finished. None if unknown."""
    with _lock:
        reply = _replies.get(request_id)
        if reply is None:
            return None
        return [dict(event) for event in reply.events[after:]], reply.finished_at is not None


def reset_for_tests() -> None:
    with _lock:
        _replies.clear()
