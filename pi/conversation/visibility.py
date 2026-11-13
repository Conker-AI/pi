"""One rule for which chats Pi may show or use: a chat that is forgotten or in the Trash is hidden.

Forgetting (pi.conversation.forgetting) removes a chat for good; the Trash (archive.trash) hides
it until the owner restores it, and a chat queued to be deleted forever is put in the Trash at
once (archive.hide). Every read path and every model input that can carry a chat's words asks
this module, so "forgotten" has one meaning instead of one spelling per module:

- `hidden_sql(column)`: an SQL condition that is true when `column` names a hidden chat; use
  `NOT ...` to keep the visible ones. It has no parameters, so it fits any query.
- `hidden(db, ids)`: the hidden chats among `ids`.
- `hidden_since()`: a query of `(id, at)`, each hidden chat with the earliest time it was hidden
  (put in the Trash or forgotten then, or made from such a chat), so a reader that left hidden
  chats out at a time can tell what it never read. It refers to no outer column, so SQLite
  builds it once per statement, not once per row (`hidden_by` is the one-column form).
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
# `{at}` is empty for the plain tree, or ", h.at" to carry the time down (`hidden_since`).
_BELOW = "SELECT s.id{at} FROM sessions s JOIN {tree} h ON s.parent_id=h.id"
# Same test as pi.schedule.jobs.ADOPTED, inlined: conversation does not import schedule.
_JOB_CHAT = (
    "SELECT j.session_id{at} FROM agent_job_sessions j JOIN keep_results k "
    "ON k.kind='ai_job' AND k.target_id=j.job_id "
    "JOIN scheduled_jobs sj ON sj.id=j.job_id "
    "AND json_extract(sj.definition,'$.createdBy') IS NOT 'owner' "
    "JOIN {tree} h ON h.id=k.session_id"
)
_SINCE = "hidden_since_tree"
# Roots with the time each was hidden: the latest move to the Trash, or the forgetting.
_ROOTS_AT = (
    "SELECT session_id, trashed_at FROM session_trash",
    (
        "SELECT f.session_id, r.forgotten_at FROM forgotten_sessions f "
        "JOIN forgetting_receipts r ON r.id=f.receipt_id"
    ),
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
    parts = [*roots, *_arms(_TREE, "", db)]
    return f"WITH RECURSIVE {_TREE}(id) AS ({' UNION '.join(parts)}) SELECT id FROM {_TREE}"


def _arms(tree: str, at: str, db=None) -> list[str]:
    arms = [_BELOW.format(tree=tree, at=at)]
    if db is None or all(_exists(db, table) for table in _JOB_TABLES):
        arms.append(_JOB_CHAT.format(tree=tree, at=at))
    return arms


def _exists(db, table: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is not None


def hidden_sql(column: str) -> str:
    """`column IN (every hidden chat)`; `column` is a column name, never input."""
    if not _COLUMN.match(column):
        raise ValueError(f"Not a column name: {column!r}")
    return f"{column} IN ({_sources()})"


def hidden_since() -> str:
    """Each chat in the Trash or forgotten, or made from one, as `(id, at)`: when it was hidden.

    One recursive query carries each root's time down the tree (a fork or a job chat gets its
    source's time), then keeps the earliest per chat: a chat is hidden at time T exactly when
    one of its roots was put in the Trash or forgotten at or before T. A chat trashed, restored
    and trashed again counts only from the later time (session_trash keeps the latest), so a
    reader may hide more than it read. It has no outer reference: use it as a CTE (MATERIALIZED)
    and compare `at` per row, so SQLite builds the tree once per statement.
    """
    parts = [*_ROOTS_AT, *_arms(_SINCE, ", h.at")]
    return (
        f"WITH RECURSIVE {_SINCE}(id, at) AS ({' UNION '.join(parts)}) "
        f"SELECT id, MIN(at) AS at FROM {_SINCE} GROUP BY id"
    )


def hidden_by(column: str, time_sql: str) -> str:
    """`column IN (every chat already hidden at time_sql)`; both are column names, never input.

    The one-column form of `hidden_since`. With an outer `time_sql` it is a correlated subquery
    that rebuilds the tree per row: a query over many rows joins `hidden_since` instead.
    """
    for name in (column, time_sql):
        if not _COLUMN.match(name):
            raise ValueError(f"Not a column name: {name!r}")
    return f"{column} IN (SELECT id FROM ({hidden_since()}) WHERE at <= {time_sql})"


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
