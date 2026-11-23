"""Shared builders for the artifact tests: a temporary store, an owner artifact, a source message."""

import pytest

from pi.content import artifacts as a
from pi.conversation.store import Store

OWNER = "artifact-owner-control-key-" + "o" * 32


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "artifact-tests.db")
    with value._connect() as db:
        db.executescript(a.SCHEMA)
    yield value
    value.close()


def owner(store, data=None):
    return a.create(
        store, a.Create(title="CON", content=data or {"kind": "markdown", "text": "original"})
    )


def privacy(db, session_id):
    return {"memoryDisabled": True, "harnessDisabled": False}


def response(store):
    session = store.create_session()
    turn = store.start_turn(session)
    message = store.complete_turn(turn, "private source")
    return a.FromMessage(title="private title", sessionId=session, messageId=message["id"])
