"""One rule for which chats Pi may show or use: a chat that is forgotten or in the Trash is hidden.

Forgetting (pi.conversation.forgetting) removes a chat for good; the Trash (archive.trash) hides
it until the owner restores it, and a chat queued to be deleted forever is put in the Trash at
once (archive.hide). Every read path and every model input that can carry a chat's words asks
this module, so "forgotten" has one meaning instead of one spelling per module:

- `hidden_sql(column)`: an SQL condition that is true when `column` names a hidden chat; use
  `NOT ...` to keep the visible ones. It has no parameters, so it fits any query.
- `hidden(db, ids)`: the hidden chats among `ids`.
- `kept_from_hidden(db)`: ids of things Conker kept (memories, jobs, agents, teams) whose chat
  is hidden, so recall can drop a kept memory that carries no chat citation.

What still shows a hidden chat: the Trash view itself, the chat opened by its own link, and
approvals waiting for the owner's answer (hiding one would leave it pending for ever). A
restored chat is visible again everywhere; nothing here deletes anything.
"""

from __future__ import annotations

import re

_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_TABLES = ("forgotten_sessions", "session_trash")


def _sources(db=None) -> str:
    """Every hidden chat id as one column named `id`; with `db`, missing tables are skipped."""
    parts = ["SELECT id FROM sessions WHERE status='forgotten'"]
    for table in _TABLES:
        if db is None or _exists(db, table):
            parts.append(f"SELECT session_id FROM {table}")
    return " UNION ".join(parts)


def _exists(db, table: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is not None


def hidden_sql(column: str) -> str:
    """`column IN (every forgotten or trashed chat)`; `column` is a column name, never input."""
    if not _COLUMN.match(column):
        raise ValueError(f"Not a column name: {column!r}")
    return f"{column} IN ({_sources()})"


def hidden(db, session_ids) -> set[str]:
    """The listed chats that are forgotten or in the Trash."""
    ids = sorted({value for value in session_ids if isinstance(value, str) and value})
    if not ids:
        return set()
    marks = ",".join("?" * len(ids))
    query = f"SELECT id FROM ({_sources(db)}) WHERE id IN ({marks})"
    return {row[0] for row in db.execute(query, ids)}


def kept_from_hidden(db, kind: str | None = None) -> set[str]:
    """Targets Conker kept from a hidden chat (keep_results), optionally of one kind."""
    if not _exists(db, "keep_results"):
        return set()
    query = f"SELECT target_id FROM keep_results WHERE {hidden_sql('session_id')}"
    params: tuple = ()
    if kind is not None:
        query += " AND kind=?"
        params = (kind,)
    return {row[0] for row in db.execute(query, params)}
