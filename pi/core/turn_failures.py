"""How a turn fails: TurnFailed, and ActedWithoutReply when the action ran but no reply arrived."""

from __future__ import annotations


class TurnFailed(RuntimeError):
    def __init__(self, reason: str, turn_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.turn_id = turn_id


class ActedWithoutReply(TurnFailed):
    """The action ran. The reply did not arrive.

    A distinct type because it must not be handled like a failure. Nothing here
    may be retried as a whole: the approval is spent and the world has already
    changed, so a caller that retried would either be refused or, worse, do the
    thing twice. Only the reply is missing, and only the reply may be asked for
    again - which is what resuming such a turn does.
    """

    def __init__(self, turn_id: str, cause: str) -> None:
        super().__init__(f"the action ran, but no reply arrived: {cause}")
        self.turn_id = turn_id
        self.cause = cause
