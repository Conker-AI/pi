"""The retired workflow offers and chat drafts: no routes, no Inbox items, rows leave with the chat."""

import sqlite3
import time
import uuid
from contextlib import closing

from fastapi.testclient import TestClient

from pi.agents import inbox, retired_workflows
from pi.api import server
from pi.conversation import forgetting
from pi.conversation.store import Store


def rows(store, session=None):
    """One open row in each retired table for the chat's latest message, as Pi used to write."""
    session = session or store.create_session()
    message = store.append_message(session, "user", "Every morning tell me the weather")["id"]
    with store._connect() as db:
        db.execute(
            "INSERT INTO workflow_offers VALUES (?,?,?,'open',?,NULL)",
            ("wfo_" + uuid.uuid4().hex, session, message, time.time()),
        )
        db.execute(
            "INSERT INTO workflow_proposals VALUES (?,?,?,?,?,1,'open',?,NULL)",
            (
                "wfp_" + uuid.uuid4().hex,
                session,
                message,
                "morning-weather",
                "Morning weather",
                time.time(),
            ),
        )
        db.commit()
    return session


def counts(store):
    with store._connect() as db:
        return [
            db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("workflow_offers", "workflow_proposals")
        ]


def test_the_tables_stay_but_the_inbox_lists_nothing_from_them(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        rows(store)
        assert counts(store) == [1, 1]
        kinds = {item["kind"] for item in inbox.inbox(store)["needsYou"]}
        assert not kinds & {"workflow", "workflow-offer"}


def test_forgetting_the_chat_removes_its_rows(tmp_path):
    """Two chats A and B with rows in both retired tables; forgetting A clears A's rows only."""
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session_a = rows(store)
        session_b = rows(store)
        assert counts(store) == [2, 2]
    # Forget chat A through the real forgetting path, not by calling redact directly.
    forgetting.forget(path, session_a, forgetting.preview(path, session_a)["confirmation"])
    with closing(Store(path)) as store:
        # A's rows are gone from both tables; B's rows remain.
        assert counts(store) == [1, 1]
        with store._connect() as db:
            left_offer = db.execute("SELECT session_id FROM workflow_offers").fetchone()[0]
            left_proposal = db.execute("SELECT session_id FROM workflow_proposals").fetchone()[0]
        assert left_offer == session_b
        assert left_proposal == session_b


def test_redact_with_empty_list_changes_nothing(tmp_path):
    """redact(db, []) is a no-op: rows remain untouched."""
    with closing(Store(tmp_path / "pi.db")) as store:
        session = rows(store)
        assert counts(store) == [1, 1]
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            retired_workflows.redact(db, [])
            db.commit()
        assert counts(store) == [1, 1]


def test_redact_without_tables_does_not_raise(tmp_path):
    """If both tables have been dropped, redact(db, [some_id]) does not raise."""
    with closing(Store(tmp_path / "pi.db")) as store:
        session = rows(store)
        with store._connect() as db:
            db.execute("DROP TABLE workflow_proposals")
            db.execute("DROP TABLE workflow_offers")
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            retired_workflows.redact(db, [session])
            db.commit()


def test_initialize_twice_is_harmless(tmp_path):
    """Calling initialize twice is harmless (IF NOT EXISTS)."""
    with closing(Store(tmp_path / "pi.db")) as store:
        with store._connect() as db:
            retired_workflows.initialize(db)
            retired_workflows.initialize(db)
            # Tables exist and are usable.
            assert db.execute(
                "SELECT COUNT(*) FROM workflow_proposals"
            ).fetchone()[0] == 0
            assert db.execute(
                "SELECT COUNT(*) FROM workflow_offers"
            ).fetchone()[0] == 0


def test_the_old_routes_are_gone(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        app = server.app
        app.state.store = store
        app.state.admin_key = "test-admin-key"
        client = TestClient(app)
        admin = {"X-Pi-Key": "test-admin-key"}
        for method, path in (
            ("GET", "/workflow-proposals"),
            ("POST", "/workflow-proposals"),
            ("GET", "/workflow-proposals/offers"),
            ("POST", "/workflow-proposals/published"),
            ("POST", "/workflow-drafts/draft"),
        ):
            assert client.request(method, path, json={}, headers=admin).status_code == 404
