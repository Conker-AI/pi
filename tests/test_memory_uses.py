"""Which turns used which memories (R-U3): ids only, kept through a correction or forget of the
memory, deleted with the chat."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from pi.conversation.forgetting import forget, preview
from pi.conversation.store import Store
from pi.memory import store as memory_store
from pi.memory import uses

SECRET = "the owner's private sentence"
PACKAGE = {
    "memories": [
        {"id": "mem_1", "text": SECRET, "revision": 3},
        {"id": "mem_2", "text": "another"},
        {"id": "mem_1", "text": "repeated id"},
        {"text": "no id"},
        {"id": 7, "text": "not a string"},
        {"id": "x" * 201, "text": "too long"},
    ],
    "entities": [{"id": "ent_1"}],
}


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def rows(store):
    with store._connect() as db:
        return [
            tuple(r)
            for r in db.execute(
                "SELECT turn_id, session_id, memory_id, revision, source FROM memory_uses "
                "ORDER BY memory_id, source"
            )
        ]


def turn_in(store, session):
    store.append_message(session, "user", "hello")
    return store.start_turn(session)


def test_a_saved_recall_package_notes_each_memory_id_once_and_no_words(store):
    session = store.create_session()
    turn = turn_in(store, session)
    memory_store.save_context(store, turn, "ok", PACKAGE)
    assert rows(store) == [
        (turn, session, "mem_1", 3, "context"),
        (turn, session, "mem_2", None, "context"),
    ]
    with store._connect() as db:
        assert SECRET not in "".join(str(tuple(r)) for r in db.execute("SELECT * FROM memory_uses"))


def test_a_turn_without_a_package_notes_nothing(store):
    session = store.create_session()
    memory_store.save_context(store, turn_in(store, session), "unavailable", None)
    assert rows(store) == []


def test_the_notes_survive_the_wipe_a_correction_or_forget_makes(store):
    session = store.create_session()
    turn = turn_in(store, session)
    memory_store.save_context(store, turn, "ok", PACKAGE)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        memory_store.invalidate_recall(db)
        db.commit()
    assert memory_store.context(store, turn)["package"] is None  # the words are gone
    assert [r[2] for r in rows(store)] == ["mem_1", "mem_2"]  # the use is not


def test_the_recall_tool_source_is_kept_apart_from_the_turns_own(store):
    session = store.create_session()
    turn = turn_in(store, session)
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        uses.record(db, turn, session, PACKAGE, "context")
        uses.record(db, turn, session, {"memories": [{"id": "mem_1"}]}, "recall")
        uses.record(db, turn, session, {"memories": [{"id": "mem_1"}]}, "recall")  # a repeat
        db.commit()
        with pytest.raises(ValueError):
            uses.record(db, turn, session, PACKAGE, "elsewhere")
    assert [(r[2], r[4]) for r in rows(store)] == [
        ("mem_1", "context"),
        ("mem_1", "recall"),
        ("mem_2", "context"),
    ]


def test_forgetting_a_chat_deletes_its_notes_and_keeps_the_others(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        root = store.create_session()
        child = store.create_session(parent_id=root)
        other = store.create_session()
        for session in (root, child, other):
            memory_store.save_context(store, turn_in(store, session), "ok", PACKAGE)
        assert len(rows(store)) == 6
    forget(path, root, preview(path, root)["confirmation"])
    with closing(sqlite3.connect(path)) as db:
        left = db.execute("SELECT DISTINCT session_id FROM memory_uses").fetchall()
    assert [r[0] for r in left] == [other]


def test_forgetting_works_on_a_database_from_before_the_table(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        root = store.create_session()
        store.append_message(root, "user", SECRET)
    with closing(sqlite3.connect(path)) as db:
        db.execute("DROP TABLE memory_uses")
        db.commit()
    assert forget(path, root, preview(path, root)["confirmation"])
