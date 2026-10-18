"""Forgetting a chat deletes its review data: judgements, votes, fact checks, decisions, names,
output settings and summary coverage."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing

from pi.conversation.forgetting import forget, preview
from pi.conversation.store import Store

SECRET = "private-review-3c1e-никому"
TABLES = (
    "response_judgements",
    "response_votes",
    "fact_checks",
    "context_items",
    "context_item_revisions",
    "session_titles",
    "session_output",
    "summary_coverage",
)


def seed(store, session_id, text):
    """One row in every review table, the free text carrying `text`."""
    answer = store.append_message(session_id, "assistant", "an answer")["id"]
    now = time.time()
    with store._connect() as db:
        db.execute("INSERT INTO response_families VALUES(?,?,?,1)", (answer, session_id, answer))
        db.execute(
            "INSERT INTO response_judgements VALUES(?,?,?,?,?,?,?)",
            (
                "judge_" + session_id,
                session_id,
                answer,
                "hash",
                "a",
                json.dumps({"scores": [{"message_id": answer, "score": 7, "reason": text}]}),
                now,
            ),
        )
        db.execute(
            "INSERT INTO response_votes VALUES(?,?,?,?,?)", (answer, session_id, answer, "up", now)
        )
        db.execute(
            "INSERT INTO fact_checks VALUES(?,?,?,?,?,?,?)",
            (
                "check_" + session_id,
                session_id,
                answer,
                "hash",
                "a",
                json.dumps({"claims": [{"claim": text, "mark": "no-source"}]}),
                now,
            ),
        )
        db.execute(
            "INSERT INTO context_items VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "dec_" + session_id[-16:],
                session_id,
                "decision",
                text,
                "owner",
                "[]",
                "active",
                None,
                now,
                now,
            ),
        )
        db.execute("INSERT INTO context_item_revisions VALUES(?,1)", (session_id,))
        db.execute("INSERT INTO summary_coverage VALUES(?,1,?)", (session_id, answer))
        db.execute("INSERT INTO session_titles VALUES(?,1,'auto','a',?)", (session_id, now))
        db.execute(
            "INSERT INTO session_output VALUES(?,1,?)",
            (session_id, json.dumps({"format": "table", "length": "short", "language": "ru"})),
        )
        db.commit()


def count(db, table, session_id):
    return db.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id=?", (session_id,)).fetchone()[
        0
    ]


def test_review_data_is_deleted_with_the_chat_and_its_branches(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        root = store.create_session(title="root")
        child = store.create_session(parent_id=root, title="branch")
        other = store.create_session(title="unrelated")
        seed(store, root, SECRET)
        seed(store, child, SECRET)
        seed(store, other, "keep this")
    forget(path, root, preview(path, root)["confirmation"])
    with closing(sqlite3.connect(path)) as db:
        for table in TABLES:
            assert count(db, table, root) == 0, table
            assert count(db, table, child) == 0, table
            assert count(db, table, other) == 1, table
    # Scan the database and its sidecars, not just query results.
    for file in path.parent.glob("pi.db*"):
        assert SECRET.encode() not in file.read_bytes(), file.name


def test_forgetting_works_on_a_store_without_the_review_tables(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        root = store.create_session(title="old")
        store.append_message(root, "user", SECRET)
    with closing(sqlite3.connect(path)) as db:
        for table in TABLES:
            db.execute(f"DROP TABLE {table}")
        db.commit()
    receipt = forget(path, root, preview(path, root)["confirmation"])
    assert root in receipt["session_ids"]
