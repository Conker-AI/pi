"""Browsers that signed in here before, and the sign-in each one made last.

A browser that signs in gets a known-device token (`store.AuthStore.login`; kept here only hashed,
sent back to /auth only) and a fresh one at every sign-in. Each token is linked to the sign-in it
came with, so when the same browser signs in again, the devices with notifications on
(gateway/push.py) that belong to its last sign-in move to the new one: signing out there turns them
off even when the page never sends its subscription again (`renew`). Only the token the browser
was given last carries them, and only while it is remembered; a browser that never signed in here,
or whose token was forgotten, carries nothing. A sign-in that idles out keeps its devices, by
agreement: notifications are for a closed tab.

The link is a table of its own: an older gateway writes `known_devices` with three values and
never reads this one, so a rollback keeps signing in, and its sign-ins move devices only by renewal.
"""

from __future__ import annotations

import sqlite3

ROWS = 20  # browsers remembered at most; the least recently used ones are forgotten
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS known_device_signins ("
    "token_hash TEXT PRIMARY KEY, session_id TEXT NOT NULL)"
)


def signed_in(
    db: sqlite3.Connection, presented: str | None, kept: str, session: str, now: float
) -> None:
    """A sign-in from a browser that sent the hashed token `presented` (None: no token) and now
    keeps the hashed token `kept`; in the login's own write transaction."""
    if presented:
        last = db.execute(
            "SELECT link.session_id FROM known_device_signins AS link JOIN known_devices AS known"
            " ON known.token_hash=link.token_hash WHERE link.token_hash=?",
            (presented,),
        ).fetchone()
        if last:
            db.execute(
                "UPDATE push_subscriptions SET session_id=? WHERE session_id=?", (session, last[0])
            )
        # The presented token stops counting.
        db.execute("DELETE FROM known_devices WHERE token_hash=?", (presented,))
    db.execute("INSERT INTO known_devices VALUES(?,?,?)", (kept, now, now))
    db.execute("INSERT INTO known_device_signins VALUES(?,?)", (kept, session))
    db.execute(
        "DELETE FROM known_devices WHERE token_hash NOT IN "
        "(SELECT token_hash FROM known_devices ORDER BY used DESC LIMIT ?)",
        (ROWS,),
    )
    forget(db)


def forget(db: sqlite3.Connection) -> None:
    """A link goes with its token: sign-out everywhere, a new password, 400 days unused or one
    browser too many. Until then a lookup joins `known_devices`, so a stale link carries nothing."""
    db.execute(
        "DELETE FROM known_device_signins WHERE token_hash NOT IN "
        "(SELECT token_hash FROM known_devices)"
    )
