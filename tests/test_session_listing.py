"""The bounded session list uses its creation index without rewriting history."""

import hashlib
import sqlite3
from contextlib import closing, contextmanager

from fastapi.testclient import TestClient

from pi import api
from pi.store import Store


def populate(store):
    with store._connect() as db:
        # Tied creation times and reverse IDs detect unintended tie-break changes.
        db.executemany(
            "INSERT INTO sessions(id,title,status,created_at,summary) VALUES(?,?,?,?,?)",
            [
                (f"ses_{300 - index:04}", f"Title {index}", "open", index // 3, f"Summary {index}")
                for index in range(300)
            ],
        )


def capture_list_query(store, monkeypatch, limit):
    statements = []
    connect = store._connect

    @contextmanager
    def traced():
        with connect() as db:
            db.set_trace_callback(statements.append)
            yield db

    with monkeypatch.context() as patch:
        patch.setattr(store, "_connect", traced)
        rows = store.list_sessions(limit=limit)
    (query,) = [
        statement for statement in statements if statement.startswith("SELECT * FROM sessions")
    ]
    with connect() as db:
        plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + query)]
    assert any("USING INDEX sessions_by_created_at" in step for step in plan)
    assert not any("TEMP B-TREE" in step for step in plan)
    return rows


def test_actual_bounded_list_query_uses_creation_index(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        populate(store)
        rows = capture_list_query(store, monkeypatch, 200)
        assert len(rows) == 200 and len({row["id"] for row in rows}) == 200
        assert [row["created_at"] for row in rows] == sorted(
            (row["created_at"] for row in rows), reverse=True
        )
        assert rows[0]["created_at"] == 99 and rows[-1]["created_at"] == 33
        assert capture_list_query(store, monkeypatch, 7) == rows[:7]
        assert all(row["summary"].replace("Summary", "Title") == row["title"] for row in rows)


def test_existing_database_migration_retains_data_and_tied_time_order(tmp_path, monkeypatch):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as original:
        populate(original)
        original.append_message("ses_0001", "user", "Retained conversation")
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute("DROP INDEX sessions_by_created_at")
        db.execute("DROP INDEX sessions_history_order")
        old_query = "SELECT * FROM sessions ORDER BY created_at DESC LIMIT 200"
        expected = [dict(row) for row in db.execute(old_query)]
        old_plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + old_query)]
        assert any("TEMP B-TREE" in step for step in old_plan)
    for _ in range(2):
        with closing(Store(path)) as migrated:
            assert capture_list_query(migrated, monkeypatch, 200) == expected
            assert migrated.messages("ses_0001")[0]["content"] == "Retained conversation"
            with migrated._connect() as db:
                assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 300
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='sessions_by_created_at'"
                    ).fetchone()[0]
                    == 1
                )


def test_list_api_retains_bounds_runtime_auth_and_projection(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        populate(store)
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "recovery-key", raising=False)
        monkeypatch.setattr(
            api.app.state,
            "gateway_key_hash",
            hashlib.sha256(b"runtime-key").hexdigest(),
            raising=False,
        )
        client = TestClient(api.app)
        headers = {"X-Pi-Gateway-Key": "runtime-key"}
        assert client.get("/sessions").status_code == 401
        for suffix, limit in (("", 50), ("?limit=0", 1), ("?limit=7", 7), ("?limit=500", 200)):
            response = client.get("/sessions" + suffix, headers=headers)
            assert response.status_code == 200
            assert response.json() == {"results": store.list_sessions(limit)}
