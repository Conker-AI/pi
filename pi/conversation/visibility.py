"""One rule for which chats Pi may show or use: a chat that is forgotten or in the Trash is hidden.

Forgetting (pi.conversation.forgetting) removes a chat for good; the Trash (archive.trash) hides
it until the owner restores it, and a chat queued to be deleted forever is put in the Trash at
once (archive.hide). Every read path and every model input that can carry a chat's words asks
this module, so "forgotten" has one meaning instead of one spelling per module:

- `hidden_sql(column)`: an SQL condition that is true when `column` names a hidden chat; use
  `NOT ...` to keep the visible ones. It has no parameters, so it fits any query.
- `hidden(db, ids)`: the hidden chats among `ids`.
- `hidden_by(column, time_sql)`: true when `column` names a chat that was already hidden at
  `time_sql` (put in the Trash or forgotten then, or made from such a chat), so a reader that
  left hidden chats out at that time can tell what it never read.
- `kept_from_hidden(db)`: ids of things Conker kept (memories, jobs, agents, teams) whose chat
  is hidden, so recall can drop a kept memory that carries no chat citation.

A chat made from a hidden chat is hidden with it: each fork, message branch, call, team role or
agent call below it (sessions.parent_id, all the way down: they carry its title, its summary or
its words), as forgetting forgets that whole tree and delete-forever hides it; and the chat of
an AI job Conker kept from it (the chat is named after the job) while the job is still Conker's:
once the owner adopted it (agent_jobs.adopt) the job is listed and runs as his, so its chat, its
notes and its runs show too (pi.schedule.jobs.ADOPTED). Only the chats themselves are in
session_trash, so the Trash view lists only what the owner put there.

What still shows a hidden chat: the Trash view itself, the chat opened by its own link (a
descendant's own turns see its own summary), and approvals waiting for the owner's answer
(hiding one would leave it pending for ever). A restored chat is visible again everywhere, its
descendants and its jobs' chats too; nothing here deletes anything.
"""

from __future__ import annotations

import re

_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_TABLES = ("forgotten_sessions", "session_trash")
# Recursive arms: a chat below a hidden one, and the chat of an AI job kept from a hidden one.
_TREE = "hidden_tree"
_BELOW = f"SELECT s.id FROM sessions s JOIN {_TREE} h ON s.parent_id=h.id"
# Same test as pi.schedule.jobs.ADOPTED, inlined: conversation does not import schedule.
_JOB_CHAT = (
    "SELECT j.session_id FROM agent_job_sessions j JOIN keep_results k "
    "ON k.kind='ai_job' AND k.target_id=j.job_id "
    "JOIN scheduled_jobs sj ON sj.id=j.job_id "
    "AND json_extract(sj.definition,'$.createdBy') IS NOT 'owner' "
    f"JOIN {_TREE} h ON h.id=k.session_id"
)
_JOB_TABLES = ("keep_results", "agent_job_sessions", "scheduled_jobs")


def _sources(db=None) -> str:
    """Every hidden chat id as one column named `id`; with `db`, missing tables are skipped."""
    parts = ["SELECT id FROM sessions WHERE status='forgotten'"]
    for table in _TABLES:
        if db is None or _exists(db, table):
            parts.append(f"SELECT session_id FROM {table}")
    return _tree(parts, db)


def _tree(roots: list[str], db=None) -> str:
    """`roots` and every chat made from one, as one column named `id`.

    One recursive query: every chat below one and the chat of each AI job Conker kept from one,
    until nothing new is found (UNION drops repeats, so it ends)."""
    parts = [*roots, _BELOW]
    if db is None or all(_exists(db, table) for table in _JOB_TABLES):
        parts.append(_JOB_CHAT)
    return f"WITH RECURSIVE {_TREE}(id) AS ({' UNION '.join(parts)}) SELECT id FROM {_TREE}"


def _exists(db, table: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is not None


def hidden_sql(column: str) -> str:
    """`column IN (every hidden chat)`; `column` is a column name, never input."""
    if not _COLUMN.match(column):
        raise ValueError(f"Not a column name: {column!r}")
    return f"{column} IN ({_sources()})"


def hidden_by(column: str, time_sql: str) -> str:
    """`column IN (every chat already hidden at time_sql)`; both are column names, never input.

    The roots are the chats put in the Trash or forgotten at or before that time, grown like
    `hidden_sql`. A chat trashed then, restored and trashed again later counts as hidden only
    from the later time (session_trash keeps the latest), so a reader may hide more than it read.
    """
    for name in (column, time_sql):
        if not _COLUMN.match(name):
            raise ValueError(f"Not a column name: {name!r}")
    roots = [
        f"SELECT session_id FROM session_trash WHERE trashed_at <= {time_sql}",
        (
            "SELECT f.session_id FROM forgotten_sessions f "
            "JOIN forgetting_receipts r ON r.id=f.receipt_id "
            f"WHERE r.forgotten_at <= {time_sql}"
        ),
    ]
    return f"{column} IN ({_tree(roots)})"


def hidden(db, session_ids) -> set[str]:
    """The listed chats that are hidden: forgotten, in the Trash, or made from such a chat."""
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
