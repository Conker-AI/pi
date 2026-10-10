"""Connect durable owner stops to the exact foreground provider operation.

Only active inference transports are interrupted. Tool effects keep their own
reconciliation path, and the durable guard still decides whether a result may
be committed. Registry entries never outlive the call that owns them.
"""

import threading
from contextlib import contextmanager

from . import inference_control

_lock = threading.Lock()
_active = {}


def _keys(store, execution):
    return [
        (store.path, kind, identity)
        for kind, identity in (
            ("turn", (execution or {}).get("turnExecutionId")),
            ("submission", (execution or {}).get("submissionExecutionId")),
        )
        if identity
    ]


def cancel(store, *, turn_id=None, request_id=None, requested_at=None):
    """Called only after the stop has committed; never cancel another identity."""
    keys = _keys(store, {"turnExecutionId": turn_id, "submissionExecutionId": request_id})
    with _lock:
        tokens = {
            token
            for key in keys
            for token, allowed_stop in _active.get(key, ())
            if requested_at is None or allowed_stop is None or requested_at > allowed_stop
        }
    for token in tokens:
        token.cancel("owner")


@contextmanager
def scope(store, execution):
    # Thinking already owns a cancellable token and its separate budget/registry.
    # Nested calls must preserve that owner instead of replacing its control.
    keys = _keys(store, execution)
    if inference_control.current() is not None or not keys:
        yield
        return

    from . import turn_control
    from .providers import ProviderUnavailable

    token = inference_control.Token()
    # An explicitly resumed reply can permit a previous stop. Its permission
    # is immutable and exact; delayed delivery of that stop must not cancel it.
    allowed_stop = None
    if (execution or {}).get("replyRecoveryId"):
        with store._connect() as db:
            permission = db.execute(
                "SELECT cancelled_at FROM turn_reply_recoveries WHERE request_id=? AND turn_id=?",
                (execution["replyRecoveryId"], execution.get("turnExecutionId")),
            ).fetchone()
        allowed_stop = permission[0] if permission else None
    registration = (token, allowed_stop)
    with _lock:
        for key in keys:
            _active.setdefault(key, set()).add(registration)
    try:
        with inference_control.bind(token):
            # Covers a stop committed before registration. Stops after this
            # check see the registered token and close only its transport.
            turn_control.guard(store, execution)
            yield
            token.check()
            turn_control.guard(store, execution)
    except inference_control.InferenceCancelled as exc:
        raise ProviderUnavailable(
            "Owner stopped this inference; completed effects are retained."
        ) from exc
    finally:
        with _lock:
            for key in keys:
                tokens = _active.get(key)
                if tokens is not None:
                    tokens.discard(registration)
                    if not tokens:
                        del _active[key]
