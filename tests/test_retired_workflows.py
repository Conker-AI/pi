"""The retired workflow offers and chat drafts: no routes, no Inbox items, rows leave with the chat."""

import time
import uuid
from contextlib import closing

from fastapi.testclient import TestClient

from pi.agents import inbox
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
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        session = rows(store)
        kept = rows(store)
    # Forgetting needs the database to itself, as when every Pi process is stopped.
    forgetting.forget(path, session, forgetting.preview(path, session)["confirmation"])
    with closing(Store(path)) as store:
        assert counts(store) == [1, 1]
        with store._connect() as db:
            left = db.execute("SELECT session_id FROM workflow_offers").fetchone()[0]
        assert left == kept


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
