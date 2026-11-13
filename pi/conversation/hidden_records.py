"""Which MemoryGate records come from a hidden chat (pi.conversation.visibility), found from Pi's
own tables without asking MemoryGate. One copy for every reader: recall at the start of a turn,
`conker.recall` and its saved results, a retry's saved input, the memory explorer and owner search.

A record is hidden when it cites a hidden chat or one of its messages, or when Pi can name it by id:
- each message Pi delivered becomes evidence, an analysis and maybe a memory whose ids derive from
  the message id and the receiving agent (`receipt_ids`);
- a memory Conker kept on its own carries no chat at all: keep_results names it;
- a memory a hidden chat rewrote through a memory proposal keeps its id but its text came from
  that chat, so it is hidden while the edit was applied or may have been (applying, unknown).

`own` is the chat the reading is for. A chat in the Trash opened by its own id still recalls its
own records, so with `own` set that chat counts as visible here; another hidden chat's records stay
out, and a forgotten chat never counts as visible. The owner's library and search pass no `own`.
Restoring a chat brings everything back; nothing here deletes anything.
"""

from __future__ import annotations

from functools import lru_cache
from uuid import NAMESPACE_URL, uuid5

from pi.conversation import visibility

# The MemoryGate kind of a recall package group's records, where Pi can name a hidden one by id.
RECORD_KINDS = {"memories": "memory", "evidence": "evidence"}
# Derived ids kept in memory: a history re-checks every saved recall result against every message
# of every hidden chat, so the same formula would run once per result per message. The formula is
# pure (no state, no clock), so a cached answer is never stale; it bounds memory, not correctness.
_DERIVED_CACHE = 32768


@lru_cache(maxsize=_DERIVED_CACHE)
def receipt_ids(agent_id: str, message_id: str) -> tuple[str, str, str]:
    """MemoryGate's ids for one delivered message (the formula in delivery.validate_receipt and
    MemoryGate's conversation_memory): the evidence record is the receipt id; its analysis and its
    memory derive from it."""
    receipt = str(uuid5(NAMESPACE_URL, f"pi:{agent_id}:{message_id}"))
    analysis = str(uuid5(NAMESPACE_URL, receipt + ":analysis"))
    return receipt, analysis, str(uuid5(NAMESPACE_URL, receipt + ":memory"))


def _table(db, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() is not None


def _exempt(db, own) -> str | None:
    """`own` when it may count as visible: a chat in the Trash yes, a forgotten chat never."""
    if not isinstance(own, str) or not own:
        return None
    if db.execute("SELECT 1 FROM sessions WHERE id=? AND status='forgotten'", (own,)).fetchone():
        return None
    if (
        _table(db, "forgotten_sessions")
        and db.execute("SELECT 1 FROM forgotten_sessions WHERE session_id=?", (own,)).fetchone()
    ):
        return None
    return own


def _hidden_where(column: str, own: str | None) -> tuple[str, tuple]:
    """SQL condition and parameters: `column` names a hidden chat other than `own`."""
    condition = visibility.hidden_sql(column)
    if own is None:
        return condition, ()
    return f"{condition} AND {column}<>?", (own,)


# Messages whose MemoryGate records may exist. A message with no outbox row was never sent (only
# the outbox delivers, and a row is removed only before delivery starts), so it names nothing. A
# delete MemoryGate acknowledged ('delete' + 'sent': forgetting the chat queued it) erased the
# message's memory and analysis and invalidated its evidence, which recall and the explorer skip;
# no saved package can still hold them, since forgetting cleared every saved package and none is
# saved while a delete is owed (pi.memory.store.recall_held_in). So the set stops growing with
# forgotten chats: it holds only the Trash and deletes still owed.
_DELIVERED = (
    "SELECT o.message_id, o.destination_agent_id FROM memory_outbox o "
    "JOIN messages m ON m.id=o.message_id "
    "WHERE NOT (o.operation='delete' AND o.state='sent') AND {where}"
)


def _namespaces(db) -> set[str]:
    """Every MemoryGate namespace Pi has delivered to or forgotten in: for a message sent before
    Pi pinned destinations (destination NULL), any of them may be the one that holds it."""
    found = {
        row[0]
        for row in db.execute(
            "SELECT DISTINCT destination_agent_id FROM memory_outbox "
            "WHERE destination_agent_id IS NOT NULL"
        )
    }
    if _table(db, "memory_forget_operations"):
        found |= {
            row[0] for row in db.execute("SELECT DISTINCT agent_id FROM memory_forget_operations")
        }
    return found - {None, ""}


def _records(db, agent_ids, own) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {"memory": set(), "evidence": set(), "analysis": set()}
    if _table(db, "keep_results"):
        where, params = _hidden_where("session_id", own)
        rows = db.execute(
            f"SELECT target_id FROM keep_results WHERE kind='memory' AND {where}", params
        )
        found["memory"] |= {row[0] for row in rows}
    if _table(db, "memory_proposals"):
        where, params = _hidden_where("session_id", own)
        rows = db.execute(
            "SELECT memory_id FROM memory_proposals "
            f"WHERE state IN ('applied','applying','unknown') AND {where}",
            params,
        )
        found["memory"] |= {row[0] for row in rows}
    where, params = _hidden_where("m.session_id", own)
    rows = db.execute(_DELIVERED.format(where=where), params).fetchall()
    unpinned = None
    for message_id, destination in rows:
        # A pinned destination is the one namespace the message went to (pin_destination never
        # moves it); an unpinned one may be any reader's, or any namespace Pi knows.
        if destination:
            readers = (destination,)
        else:
            if unpinned is None:
                unpinned = tuple(sorted(({*agent_ids} | _namespaces(db)) - {None, ""}))
            readers = unpinned
        for agent in readers:
            evidence, analysis, memory = receipt_ids(agent, message_id)
            found["evidence"].add(evidence)
            found["analysis"].add(analysis)
            found["memory"].add(memory)
    return found


class Reading:
    """One reading's hidden records, worked out at most once: a history build re-checks every
    saved `conker.recall` result, and each check would otherwise read every hidden message again.
    Keyed by the reading agents and the exempt chat; a new reading (the next history build, the
    next turn) starts empty, so a chat trashed or restored since is seen then."""

    def __init__(self):
        self._found: dict[tuple, dict[str, set[str]]] = {}

    def records(self, db, agent_ids, exempt) -> dict[str, set[str]]:
        key = (tuple(sorted(set(agent_ids) - {None, ""})), exempt)
        if key not in self._found:
            self._found[key] = _records(db, key[0], exempt)
        return self._found[key]


def hidden_records(db, agent_ids=(), own=None) -> dict[str, set[str]]:
    """MemoryGate record ids, by kind (memory, evidence, analysis), that come from a hidden chat
    other than `own` (see the module docstring). `agent_ids` names the reading agents, for a
    message whose MemoryGate namespace was never pinned."""
    return _records(db, tuple(agent_ids), _exempt(db, own))


def _text(value):
    return value if isinstance(value, str) and value else None


def _cited(item):
    """(session, message) pairs of an item's citations; anything not text counts as absent, so a
    malformed citation hides nothing."""
    entries = item.get("citations") if isinstance(item, dict) else None
    if not isinstance(entries, list):
        return []
    return [
        (_text(e.get("session_id")), _text(e.get("message_id")))
        for e in entries
        if isinstance(e, dict)
    ]


def filter_cited(store, groups, agent_ids=(), own=None, reading=None) -> dict[str, list]:
    """Each group's items (`groups` maps a recall package group name to its list) without the
    records of a hidden chat other than `own`: named by `hidden_records`, or citing that chat or
    one of its messages. Read on one snapshot; `reading` (a Reading) shares the named records
    across the checks of one history build."""
    pairs = [pair for items in groups.values() for item in items for pair in _cited(item)]
    messages = sorted({m for _, m in pairs if m})
    with store._connect() as db:
        db.execute("BEGIN")
        exempt = _exempt(db, own)
        owner = {}
        if messages:
            marks = ",".join("?" * len(messages))
            owner = dict(
                db.execute(f"SELECT id,session_id FROM messages WHERE id IN ({marks})", messages)
            )
        gone = visibility.hidden(db, {s for s, _ in pairs if s} | set(owner.values())) - {exempt}
        # Only memories and evidence can be named by id; with no such id there is nothing to find.
        named = any(
            isinstance(item, dict) and _text(item.get("id"))
            for key, items in groups.items()
            if key in RECORD_KINDS
            for item in items
        )
        records = (reading or Reading()).records(db, agent_ids, exempt) if named else {}

    def hidden(key, item):
        identity = _text(item.get("id")) if isinstance(item, dict) else None
        if identity in records.get(RECORD_KINDS.get(key, ""), ()):
            return True
        return any(s in gone or owner.get(m) in gone for s, m in _cited(item))

    return {key: [item for item in items if not hidden(key, item)] for key, items in groups.items()}


def filter_package(store, package, agent_ids=(), own=None, reading=None):
    """A recall package without the records of a hidden chat other than `own`; the same object
    when nothing is left out, so a caller can tell (`is`)."""
    if not isinstance(package, dict):
        return package
    groups = {
        key: package[key]
        for key in ("memories", "entities", "episodes", "evidence")
        if isinstance(package.get(key), list)
    }
    if not any(groups.values()):
        return package
    shown = filter_cited(store, groups, agent_ids, own, reading)
    if all(len(shown[key]) == len(groups[key]) for key in groups):
        return package
    return {**package, **shown}
