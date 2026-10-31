"""The artifact listing reads only what a summary shows.

List summaries must match the full views they stand in for, and the number of
stored versions must not change how much of that history the listing decodes.
"""

import pytest

from pi.content import artifacts as a
from pi.conversation.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "artifact-list-reads.db")
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


def add_version(store, artifact, text):
    return a.mutate(
        store,
        artifact["id"],
        a.Append(
            expected_revision=artifact["revision"], content={"kind": "markdown", "text": text}
        ),
    )


def test_list_summaries_match_full_views(store):
    owner(store)
    four = owner(store, {"kind": "markdown", "text": "version one"})
    for index in range(3):
        four = add_version(store, four, f"version {index + 2}")
    a.create(store, response(store), privacy)
    listed = a.list_artifacts(store, privacy)
    with store._connect() as db:
        db.execute("BEGIN")
        rows = db.execute("SELECT * FROM artifacts ORDER BY created_at,id").fetchall()
        expected = [a._summary(a._view(db, row, privacy)) for row in rows]
    assert listed == expected


def test_listing_does_not_decode_every_version(tmp_path, monkeypatch):
    loads = []
    real_loads = a.json.loads

    def counting(text):
        loads.append(text)
        return real_loads(text)

    counts = []
    for size in (2, 8):
        value = Store(tmp_path / f"artifact-{size}.db")
        with value._connect() as db:
            db.executescript(a.SCHEMA)
        artifact = owner(value)
        for index in range(size - 1):
            artifact = add_version(value, artifact, f"extra {index}")
        loads.clear()
        monkeypatch.setattr(a.json, "loads", counting)
        try:
            a.list_artifacts(value, privacy)
        finally:
            monkeypatch.undo()
            value.close()
        counts.append(len(loads))
    assert counts[0] == counts[1]
