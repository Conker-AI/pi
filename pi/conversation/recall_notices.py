"""Inbox notice "answers used a memory you changed" (R-U3 step 3).

When a memory correction or a forget finishes, Pi files one notice if earlier answers used that
memory (`pi.memory.uses`). The notice holds ids only: the memory id, whether it was corrected or
forgotten, the revision the old answers saw and the request id that is its key. It holds no
memory words and no chat id. How many answers and chats it reaches is counted when the Inbox is
read, from `memory_uses` rows in chats that are not forgotten and not in the Trash, so a chat
that leaves takes itself out of the count, and a notice that reaches nothing is not shown. No
answer is counted by two notices: a second change of the same memory tells only what the ones
before it did not. An answer saved after a memory's last change is counted by none.

Design: context/design/memory-recall-notice.md in the Conker workspace.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Literal

from pi.conversation import visibility
from pi.core import notices
from pi.core.errors import ServiceError
from pi.core.models import Strict

NOTICE_ID = r"^mrn_[0-9a-f]{32}$"
CHANGES = ("corrected", "forgotten")
MAX_CHATS = 20
MAX_OPEN = 100  # notices shown per Inbox call, newest first
STORED_TITLE = "Memory changed"  # the card's words are built when read, from visible chats
_VISIBLE = f"s.status<>'forgotten' AND NOT {visibility.hidden_sql('u.session_id')}"


class RecallNoticeError(ServiceError):
    pass


class RecallChat(Strict):
    id: str
    title: str  # "" for a chat with no name yet


class RecallSource(Strict):
    kind: Literal["memory-recall"]
    noticeId: str
    memoryId: str


class RecallNotice(Strict):
    kind: Literal["memory-recall"]
    id: str
    title: str
    since: float
    memoryId: str
    change: Literal["corrected", "forgotten", "changed"]
    answers: int
    chats: int
    sessionIds: list[str]
    sessions: list[RecallChat]  # the same chats as sessionIds, with their titles
    source: RecallSource


class RecallNoticePage(Strict):
    items: list[RecallNotice]


def _identity(request_id: str) -> str:
    return "mrn_" + uuid.uuid5(uuid.NAMESPACE_URL, f"pi-memory-recall:{request_id}").hex


def _chain(candidate: bool = False) -> str:
    """A WITH clause naming `chain`: every memory-recall notice (with `candidate`, also the one
    about to be filed: :id, :at, :memory, :revision) beside what the notices filed before it for
    the same memory (by time, dismissed or not) already counted: `low`, the highest revision one
    of them named, and `low_at`, the latest time one was filed (-1 and 0 when there is none). A
    notice whose facts are not valid (`valid` false) is never shown, so it bounds nothing."""
    filing = (
        " UNION ALL SELECT :id, :at, NULL, "
        "json_object('memoryId', :memory, 'revision', :revision), 1"
        if candidate
        else ""
    )
    return (
        "WITH recall AS (SELECT *, json_extract(facts,'$.memoryId') AS memory_id, "
        "json_extract(facts,'$.revision') AS revision, "
        "json_type(facts,'$.memoryId')='text' AND json_type(facts,'$.revision')='integer' AS valid "
        "FROM (SELECT id, created_at, dismissed_at, facts, 0 AS candidate FROM notices "
        f"WHERE kind=:kind{filing})), "
        "chain AS (SELECT *, coalesce(MAX(CASE WHEN valid THEN revision END) OVER earlier, -1) "
        "AS low, coalesce(MAX(CASE WHEN valid THEN created_at END) OVER earlier, 0) AS low_at "
        "FROM recall WINDOW earlier AS (PARTITION BY memory_id ORDER BY created_at, id "
        "ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING))"
    )


# The uses notice `c` counts: of its memory as it was (its revision or older; a use with no
# revision by time alone), recorded by the time it was filed, and not counted by an earlier
# notice: recorded after the latest one, or of a revision above any they named. Split on
# `low_at` into two arms so each reads one index range: uses recorded since the last notice, and
# older uses of a newer revision (only after a clock step). No use is counted by two notices.
# While clocks and revisions move forward together, a use is counted by the first notice filed
# after it was recorded; a use saved after a memory's last change (or its forget) by none.
_ARMS = (
    (
        "u.recorded_at>c.low_at AND u.recorded_at<=c.created_at "
        "AND (u.revision IS NULL OR u.revision<=c.revision)"
    ),
    "u.revision>c.low AND u.revision<=c.revision AND u.recorded_at<=c.low_at",
)


def _counted(where: str) -> str:
    """The uses counted by the chain notices `c` that match `where`, one row per use."""
    return " UNION ALL ".join(
        "SELECT c.id AS notice, c.created_at AS filed, c.memory_id, c.facts, u.session_id, "
        f"u.recorded_at FROM chain c JOIN memory_uses u ON u.memory_id=c.memory_id AND {arm} "
        f"WHERE {where}"
        for arm in _ARMS
    )


def file(
    db, *, request_id: str, memory_id: str, change: str, revision: int, now: float | None = None
) -> bool:
    """Note that a change finished; False when no answer used the memory or it is already filed.

    The caller owns the transaction, so the notice is saved with the change itself."""
    if change not in CHANGES:
        raise ValueError("unknown memory change")
    if (
        type(revision) is not int
        or not db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_uses'").fetchone()
    ):
        return False  # both callers hold an int (NOT NULL column); never a notice the read skips
    at = time.time() if now is None else now
    # Existence only: a chat in the Trash when the change finishes still counts, so its notice
    # appears once the chat is restored. A forgotten chat's rows are already deleted, and who may
    # be seen is decided again every time the Inbox is read.
    identity = _identity(request_id)
    if not db.execute(
        f"{_chain(candidate=True)}, counted AS ({_counted('c.candidate')}) "
        "SELECT 1 FROM counted LIMIT 1",
        {
            "kind": notices.MEMORY_RECALL,
            "id": identity,
            "at": at,
            "memory": memory_id,
            "revision": revision,
        },
    ).fetchone():
        return False
    return notices.insert(
        db,
        identity=identity,
        kind=notices.MEMORY_RECALL,
        subject=request_id,
        title=STORED_TITLE,
        created_at=at,
        facts={"memoryId": memory_id, "change": change, "revision": revision},
    )


def _title(change: str, answers: int, chats: int) -> str:
    noun = "answer" if answers == 1 else "answers"
    verb = {"corrected": "corrected", "forgotten": "forgot"}.get(change, "changed")
    base = f"A memory you {verb} was used in {answers} {noun}"
    if not chats:
        return base
    return f"{base} in {chats} {'chat' if chats == 1 else 'chats'}"


def needs_you(store) -> list[dict]:
    """Open notices that still reach a visible answer, for the Inbox (newest first).

    Two queries in one read transaction however many notices are open: the reach is counted in
    SQL before the limit, so notices that reach nothing (only chats in the Trash or forgotten)
    never hide a live one, and each notice reads only the uses it may count (index ranges)."""
    reached = f"FROM counted u JOIN sessions s ON s.id=u.session_id WHERE {_VISIBLE}"
    with store._connect() as db:
        db.execute("BEGIN")  # both reads see one snapshot
        rows = db.execute(
            f"{_chain()}, counted AS ({_counted('c.dismissed_at IS NULL AND c.valid')}) "
            "SELECT notice AS id, filed AS created_at, memory_id, "
            "json_extract(facts,'$.change') AS change, COUNT(*) AS answers, "
            f"COUNT(DISTINCT u.session_id) AS chats {reached} "
            "GROUP BY notice ORDER BY filed DESC, notice DESC LIMIT :limit",
            {"kind": notices.MEMORY_RECALL, "limit": MAX_OPEN},
        ).fetchall()
        shown: dict[str, list[dict]] = {row["id"]: [] for row in rows}
        if shown:
            marks = ",".join(f":n{index}" for index in range(len(shown)))
            for found in db.execute(
                f"{_chain()}, counted AS ({_counted(f'c.id IN ({marks})')}), "
                "per AS (SELECT notice, u.session_id, s.title, MAX(u.recorded_at) AS last "
                f"{reached} GROUP BY notice, u.session_id) "
                "SELECT notice, session_id, title FROM (SELECT *, ROW_NUMBER() OVER ("
                "PARTITION BY notice ORDER BY last DESC, session_id DESC) AS place FROM per) "
                "WHERE place<=:chats ORDER BY notice, place",
                {
                    "kind": notices.MEMORY_RECALL,
                    "chats": MAX_CHATS,
                    **{f"n{index}": identity for index, identity in enumerate(shown)},
                },
            ):
                shown[found["notice"]].append(
                    {"id": found["session_id"], "title": found["title"] or ""}
                )
        db.commit()
    return [_item(row, shown[row["id"]]) for row in rows]


def _item(row, chats: list[dict]) -> dict:
    recorded = row["change"]
    change = recorded if isinstance(recorded, str) and recorded in CHANGES else "changed"
    return {
        "kind": "memory-recall",
        "id": row["id"],
        "title": _title(change, row["answers"], row["chats"]),
        "since": row["created_at"],
        "memoryId": row["memory_id"],
        "change": change,
        "answers": row["answers"],
        "chats": row["chats"],
        "sessionIds": [chat["id"] for chat in chats],
        "sessions": chats,
        "source": {"kind": "memory-recall", "noticeId": row["id"], "memoryId": row["memory_id"]},
    }


def listing(store) -> dict:
    """The open notices for the owner's Inbox card, the same list the Inbox reads."""
    return {"items": needs_you(store)}


def dismiss(store, identity: str, *, now: float | None = None) -> dict:
    """Hide a notice; the uses stay listed at `GET /memory/{id}/uses`."""
    if not re.fullmatch(NOTICE_ID, identity or ""):
        raise RecallNoticeError("not_found", "Notice not found.", 404)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT dismissed_at FROM notices WHERE id=? AND kind=?",
            (identity, notices.MEMORY_RECALL),
        ).fetchone()
        if row is None:
            raise RecallNoticeError("not_found", "Notice not found.", 404)
        changed = row["dismissed_at"] is None
        if changed:
            db.execute(
                "UPDATE notices SET dismissed_at=? WHERE id=?",
                (time.time() if now is None else now, identity),
            )
        db.commit()
    return {"id": identity, "dismissed": True, "changed": changed}
