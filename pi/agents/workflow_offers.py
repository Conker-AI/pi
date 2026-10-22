"""Offers to draft a workflow, made when the owner asks in chat for something to run by itself.

When the owner's own message asks for something recurring ("every morning tell me the weather"),
Pi puts an offer in the Inbox and tells the companion to mention it. Nothing is drafted until the
owner accepts (`conker workflow accept`), which drafts from that message exactly as `from-chat`
does; nothing runs until they review and publish the draft. The check reads only the owner's
message, never the model's reply, tool results or web pages, so injected text cannot make an
offer. Pi keeps only the link and its state, and forgets it with the chat.
"""

# The Russian patterns are Cyrillic on purpose; letters that look Latin are not a mistake.
# ruff: noqa: RUF001

from __future__ import annotations

import re
import time
import unicodedata
import uuid

from pi.core.errors import ServiceError

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_offers (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    message_id TEXT NOT NULL UNIQUE REFERENCES messages(id),
    state TEXT NOT NULL CHECK(state IN ('open', 'accepted', 'dismissed')),
    created_at REAL NOT NULL,
    decided_at REAL
);
CREATE INDEX IF NOT EXISTS workflow_offers_open ON workflow_offers(state, created_at);
"""

_UNIT = (
    r"(?:morning|evening|night|day|weekday|weekend|week|month|year|hour|minute"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday)s?"
)
# Something that should happen again and again...
_RECURRING = re.compile(
    r"\b(?:every|each)\s+(?:other\s+|single\s+)?(?:" + _UNIT + r"|\d+\s*" + _UNIT + r")\b"
    r"|\b(?:daily|weekly|hourly|monthly|on a schedule)\b"
    r"|\bкажд(?:ое|ый|ую|ые|ой)\s+\w+"
    r"|\b(?:ежедневно|еженедельно|ежечасно|ежемесячно)\b"
)
# ...asked of the companion, not just told about.
_REQUEST = re.compile(
    r"\b(?:remind|tell|send|check|notify|text|email|message|give|show|summari[sz]e|fetch|post"
    r"|watch|track|alert|let me know|run|make|create|build|draft|set up|can you|could you"
    r"|would you|will you|i want you to|i'd like you to)\b"
    r"|\b(?:напоминай|напомни|присылай|пришли|отправляй|отправь|проверяй|проверь|сообщай|сообщи"
    r"|рассказывай|расскажи|скажи|говори|делай|сделай|создай|настрой|можешь|сможешь)\b"
)
# Asking for a workflow by name is enough on its own.
_NAMED = re.compile(
    r"\b(?:make|create|build|draft|set up)\s+(?:me\s+)?(?:a|an)\s+(?:workflow|automation)\b"
    r"|\b(?:сделай|создай|настрой)\s+(?:мне\s+)?(?:воркфлоу|автоматизацию)\b"
)
# A question about something ("why do I wake up every night?") is not a request for it.
_QUESTION = re.compile(r"\A\s*(?:why|what|how|when|who|which|почему|зачем|что|как|когда|кто)\b")
EXCERPT = 80


class OfferError(ServiceError):
    pass


def initialize(db):
    db.executescript(SCHEMA)


def asks_for_workflow(text: str) -> bool:
    """True when the owner's message asks for something recurring, or for a workflow by name."""
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    if _NAMED.search(normalized):
        return True
    return (
        not _QUESTION.match(normalized)
        and bool(_RECURRING.search(normalized))
        and bool(_REQUEST.search(normalized))
    )


def consider(store, execution, turn_id: str, text: str, *, tools: bool) -> dict | None:
    """Offer a draft for this turn's owner message when it asks for one; at most once per message.

    Only in the companion's own chats, only while the owner has tools at all (ToolGate is there
    and tools were not left out at setup), and never in a chat whose harness is off.
    """
    if (
        not tools
        or execution.get("kind") != "companion"
        or execution.get("privacy", {}).get("harnessDisabled")
        or not asks_for_workflow(text)
    ):
        return None
    with store._connect() as db:
        row = db.execute(
            "SELECT t.message_id, m.session_id FROM turn_messages t JOIN messages m "
            "ON m.id=t.message_id WHERE t.turn_id=? AND t.purpose='input' AND m.role='user'",
            (turn_id,),
        ).fetchone()
        if row is None:
            return None
        db.execute(
            "INSERT OR IGNORE INTO workflow_offers VALUES (?,?,?,'open',?,NULL)",
            ("wfo_" + uuid.uuid4().hex, row["session_id"], row["message_id"], time.time()),
        )
        db.commit()
        offer = db.execute(
            "SELECT * FROM workflow_offers WHERE message_id=?", (row["message_id"],)
        ).fetchone()
    return _view(offer)


def note(store, turn_id: str) -> str | None:
    """What the companion is told when this turn's message has an open offer."""
    with store._connect() as db:
        offer = db.execute(
            "SELECT o.id FROM workflow_offers o JOIN turn_messages t ON t.message_id=o.message_id "
            "WHERE t.turn_id=? AND t.purpose='input' AND o.state='open'",
            (turn_id,),
        ).fetchone()
    if offer is None:
        return None
    return (
        "The owner's latest message asks for something to happen by itself. Pi has put an offer "
        "in the owner's Inbox to draft a workflow from that message. Nothing is drafted, saved "
        "or run until the owner accepts the offer there, reviews the draft and publishes it. "
        "Answer the message as usual and add one short sentence saying the offer is in their "
        "Inbox. Do not say the workflow exists, is scheduled or will run."
    )


def _view(row) -> dict:
    return {
        "id": row["id"],
        "sessionId": row["session_id"],
        "messageId": row["message_id"],
        "state": row["state"],
        "createdAt": row["created_at"],
    }


def _excerpt(text) -> str:
    """The start of the owner's message on one line, without control characters."""
    flat = " ".join(
        "".join(c for c in str(text) if unicodedata.category(c)[0] != "C" or c in "\n\t").split()
    )
    return flat if len(flat) <= EXCERPT else flat[: EXCERPT - 1].rstrip() + "…"


def get(store, identity: str) -> dict:
    with store._connect() as db:
        row = db.execute("SELECT * FROM workflow_offers WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise OfferError("not_found", "Workflow offer not found.", 404)
    return _view(row)


def open_items(store) -> list[dict]:
    """Open offers whose message is still there, with the start of that message."""
    with store._connect() as db:
        rows = db.execute(
            "SELECT * FROM workflow_offers WHERE state='open' "
            "AND session_id NOT IN (SELECT session_id FROM forgotten_sessions) ORDER BY created_at"
        ).fetchall()
    items = []
    for row in rows:
        message = store.get_message(row["message_id"])
        if (
            message is None
            or message.get("content_status") == "forgotten"
            or not isinstance(message.get("content"), str)
        ):
            continue
        items.append({**_view(row), "excerpt": _excerpt(message["content"])})
    return items


def needs_you(store) -> list[dict]:
    return [
        {
            "kind": "workflow-offer",
            "id": item["id"],
            "title": "Draft a workflow from your message? " + item["excerpt"],
            "since": item["createdAt"],
            "source": {
                "kind": "workflow-offer",
                "offerId": item["id"],
                "sessionId": item["sessionId"],
                "messageId": item["messageId"],
            },
        }
        for item in open_items(store)
    ]


def accepted(db, message_id: str, now: float) -> None:
    """A draft was made from this message: its offer is done. Runs in the caller's transaction."""
    db.execute(
        "UPDATE workflow_offers SET state='accepted', decided_at=? "
        "WHERE message_id=? AND state='open'",
        (now, message_id),
    )


def dismiss(store, identity: str, *, now: float | None = None) -> dict:
    offer = get(store, identity)
    with store._connect() as db:
        db.execute(
            "UPDATE workflow_offers SET state='dismissed', decided_at=? WHERE id=? AND state='open'",
            (time.time() if now is None else now, identity),
        )
        db.commit()
    return {"id": identity, "state": "dismissed" if offer["state"] == "open" else offer["state"]}


def redact(db, session_ids: list[str]) -> None:
    """Forget with the chat, inside the caller's forgetting transaction."""
    if (
        session_ids
        and db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_offers'"
        ).fetchone()
    ):
        db.executemany(
            "DELETE FROM workflow_offers WHERE session_id=?", [(value,) for value in session_ids]
        )
