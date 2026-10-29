"""The owner's chat settings Pi reads: new-chat privacy, chat names, the queue and uploads.

Each value comes from the owner's settings document (`pi.owner.settings`), which holds
preferences, never authority. Every reader here checks its value again and uses the setting's
default when the value is missing, of the wrong type or out of range. None of them lets Conker do
more: the new-chat defaults can only make a chat more private, and the others only limit.

The readers take an open connection, so a caller already inside a transaction (creating a chat,
queueing a message) reads the document in the same snapshot as the rest of its work.
"""

from __future__ import annotations

import json

INCOGNITO = "chats.default_incognito"
HELPERS = "privacy.helpers"
NAMES_AUTO = "chats.names.auto"
NAMES_STYLE = "chats.names.style"
QUEUE = "chats.queue"
QUEUE_MAX = "chats.queue_max"
UPLOAD_LIMIT = "files.upload_limit"

NAME_STYLES = ("short", "descriptive", "emoji")
QUEUE_LIMIT = 5  # the most a chat queue ever holds; the setting picks 1 to this
UPLOAD_MAX_MB = 500
MIB = 1024 * 1024


def _value(db, setting_id):
    """One stored value, or None when the owner never set it."""
    row = db.execute("SELECT vals FROM owner_settings WHERE singleton=1").fetchone()
    return json.loads(row[0]).get(setting_id) if row else None


def _whole(value, low, high):
    """A whole number from low to high, or None. True and False are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not low <= value <= high or value != int(value):
        return None
    return int(value)


def new_chat_privacy(db) -> dict[str, bool]:
    """Restrictions a new chat starts with: Private by default turns memory off, and Helper
    models in new chats, when off, turns helpers off. Only an exact true or false counts."""
    return {
        "memoryDisabled": _value(db, INCOGNITO) is True,
        "harnessDisabled": _value(db, HELPERS) is False,
    }


def auto_names(db) -> bool:
    """Name chats automatically: on unless the owner turned it off."""
    return _value(db, NAMES_AUTO) is not False


def name_style(db) -> str:
    """Name style: short, descriptive or emoji; short for anything else."""
    value = _value(db, NAMES_STYLE)
    return value if value in NAME_STYLES else "short"


def queue_on(db) -> bool:
    """Queue messages: on unless the owner turned it off."""
    return _value(db, QUEUE) is not False


def queue_max(db) -> int:
    """Queued messages: 1 to 5 waiting in one chat; 5 for anything else."""
    return _whole(_value(db, QUEUE_MAX), 1, QUEUE_LIMIT) or QUEUE_LIMIT


def upload_limit(db) -> int | None:
    """Largest upload in bytes, or None for no limit.

    Shared files have had no size limit since the owner's decision of 2026-10-18, so a limit
    applies only when the owner chose one: 1 to 500 MB. Anything else keeps no limit.
    """
    megabytes = _whole(_value(db, UPLOAD_LIMIT), 1, UPLOAD_MAX_MB)
    return None if megabytes is None else megabytes * MIB
