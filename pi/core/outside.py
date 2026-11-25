"""The outside mark on a chat: after a webhook payload, the whole chat holds someone else's text.

Only the writer lives here, so a package below turns can set it; the reader, outside_content(),
stays in pi.turns.access.
"""

from __future__ import annotations

import time


def mark_outside(store, session_id, reason="webhook"):
    """From now on this chat holds someone else's text: every turn in it (and in its forks) counts
    as having read outside content, and none of its messages is sent to memory."""
    with store._connect() as db:
        mark_outside_in(db, session_id, reason)


def mark_outside_in(db, session_id, reason="webhook"):
    """`mark_outside` inside the caller's transaction, so the chat is never seen unmarked."""
    db.execute(
        "INSERT OR IGNORE INTO outside_sessions VALUES (?,?,?)",
        (session_id, reason, time.time()),
    )
