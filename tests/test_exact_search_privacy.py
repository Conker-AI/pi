"""Exact message search shares metadata privacy before applying its page limit."""

import json
from contextlib import closing, contextmanager

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_universal_search import client_for

from pi.context import universal_search
from pi.conversation import search as conversation_search
from pi.conversation import settings as session_settings
from pi.conversation.store import Store


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "exact.db")) as value:
        yield value


def messages(store, sid, count, *, prefix, created_at=0):
    ids = [f"msg_{prefix}_{i:04}" for i in range(count)]
    with store._connect() as db:
        db.executemany(
            "INSERT INTO messages(id,session_id,seq,role,content,created_at) VALUES(?,?,?,'user',?,?)",
            [
                (identity, sid, index, json.dumps("Needle retained text"), created_at)
                for index, identity in enumerate(ids)
            ],
        )
    return ids


def walk(store, *, query="needle", limit=25):
    cursor, rows = None, []
    while True:
        page = conversation_search.search(store, query, limit=limit, cursor=cursor)
        assert page["scope"] == "public-conversation-text"
        rows.extend(page["results"])
        cursor = page["next_cursor"]
        if cursor is None:
            return rows


def test_more_than_page_cap_private_candidates_do_not_hide_older_public_messages(store):
    public = store.create_session(title="Public")
    private = store.create_session(title="Private", privacy={"memoryDisabled": True})
    expected = messages(store, public, 65, prefix="public", created_at=0)
    messages(store, private, 251, prefix="private", created_at=1)
    rows = walk(store)
    assert [row["id"] for row in rows] == list(reversed(expected))
    assert all(row["session_id"] == public for row in rows)


def test_legacy_memory_skip_matches_shared_policy_and_metadata(store):
    legacy = store.create_session(title="Needle legacy")
    original = store.append_message(legacy, "user", "Needle legacy original")
    explicit = store.create_session(
        title="Needle explicit", privacy={"memoryDisabled": False, "harnessDisabled": False}
    )
    allowed = store.append_message(explicit, "user", "Needle explicit original")
    with store._connect() as db:
        assert (
            db.execute("SELECT 1 FROM session_settings WHERE session_id=?", (legacy,)).fetchone()
            is None
        )
        db.execute("INSERT INTO setup_optional_choices VALUES('memory',1,'choice_skip','skip',0)")
    assert [row["id"] for row in walk(store)] == [allowed["id"]]
    records, _ = universal_search.lexical(
        store, None, None, "needle", {"sources": ["conversations"]}, "metadata"
    )
    assert [row["recordId"] for row in records] == [explicit]
    # The per-call cache must not carry the former default into a later request.
    with store._connect() as db:
        db.execute(
            "INSERT INTO setup_optional_choices VALUES('memory',2,'choice_include','include',1)"
        )
    assert {row["id"] for row in walk(store)} == {allowed["id"], original["id"]}


@pytest.mark.parametrize("key", ["memoryDisabled", "harnessDisabled"])
def test_current_inherited_and_immutable_private_history_remain_excluded(store, key):
    public = store.create_session(title="Public")
    allowed = store.append_message(public, "user", "needle")
    current = store.create_session(title="Current", privacy={key: True})
    child = store.create_session(title="Inherited", parent_id=current)
    historical = store.create_session(title="Historical", privacy={key: True})
    for sid in (current, child, historical):
        store.append_message(sid, "user", "needle private original")
    setting = session_settings.load(store, historical)
    setting["settings"]["privacy"][key] = False
    session_settings.save(
        store,
        historical,
        session_settings.Update(
            expected_revision=setting["revision"],
            settings=session_settings.Settings.model_validate(setting["settings"]),
        ),
    )
    assert [row["id"] for row in walk(store)] == [allowed["id"]]


def test_ledger_only_forgotten_matches_do_not_surface_or_consume_limit(store):
    public = store.create_session(title="Public")
    forgotten = store.create_session(title="Forgotten legacy title")
    expected = messages(store, public, 3, prefix="public", created_at=0)
    messages(store, forgotten, 251, prefix="forgotten", created_at=1)
    with store._connect() as db:
        db.execute(
            "INSERT INTO forgetting_receipts VALUES('del_exact',?,0,'confirmed',0,0)", (forgotten,)
        )
        db.execute("INSERT INTO forgotten_sessions VALUES(?,'del_exact')", (forgotten,))
    assert store.get_session(forgotten)["status"] == "open"
    page = conversation_search.search(store, "needle", limit=3)
    assert [row["id"] for row in page["results"]] == list(reversed(expected))
    assert page["next_cursor"] is None


def test_eligibility_uses_one_connection_bounded_cache_and_bounded_fetch(store, monkeypatch):
    public = store.create_session(title="Public")
    messages(store, public, 1000, prefix="public")
    connect, policy = store._connect, session_settings.source_privacy
    connections, callbacks, policies, fetched, statements = [], [], [], [], []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            rows = self.cursor.fetchall()
            fetched.append(len(rows))
            assert len(rows) <= 51
            return rows

    class Connection:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def create_function(self, name, arity, function, **kwargs):
            if name == "search_session_eligible":
                callbacks.append(function)
            return self.db.create_function(name, arity, function, **kwargs)

        def execute(self, statement, *args):
            cursor = self.db.execute(statement, *args)
            if "SELECT m.id,m.session_id" in statement:
                assert statement.endswith("LIMIT ?") and args[0][-1] == 51
                return Cursor(cursor)
            return cursor

    @contextmanager
    def hooked():
        with connect() as db:
            db.set_trace_callback(statements.append)
            connection = Connection(db)
            connections.append(connection)
            yield connection

    def checked(db, identity):
        assert db is connections[-1]
        policies.append(identity)
        return policy(db, identity)

    monkeypatch.setattr(store, "_connect", hooked)
    monkeypatch.setattr(session_settings, "source_privacy", checked)
    with connect() as monitor:
        version = monitor.execute("PRAGMA data_version").fetchone()[0]
        page = conversation_search.search(store, "needle", limit=50)
        assert monitor.execute("PRAGMA data_version").fetchone()[0] == version
    assert len(page["results"]) == 50 and page["next_cursor"]
    assert len(connections) == 1 and policies == [public] and fetched == [51]
    assert callbacks[0].cache_info().maxsize == 256
    assert "BEGIN" in statements
    assert not any(
        statement.lstrip(" -").startswith(("INSERT", "UPDATE", "DELETE"))
        for statement in statements
    )


def test_privacy_cache_cannot_grow_with_unbounded_matching_sessions(store, monkeypatch):
    with store._connect() as db:
        db.executemany(
            "INSERT INTO sessions(id,title,created_at) VALUES(?,'Private',0)",
            [(f"ses_{i}",) for i in range(600)],
        )
        settings = json.dumps(
            {"agentId": "companion", "privacy": {"memoryDisabled": True, "harnessDisabled": False}}
        )
        db.executemany(
            "INSERT INTO session_settings VALUES(?,1,?)",
            [(f"ses_{i}", settings) for i in range(600)],
        )
        db.executemany(
            "INSERT INTO messages(id,session_id,seq,role,content,created_at) VALUES(?,?,0,'user',?,0)",
            [(f"msg_{i}", f"ses_{i}", json.dumps("needle")) for i in range(600)],
        )
    connect, callbacks = store._connect, []

    class Connection:
        def __init__(self, db):
            self.db = db

        def __getattr__(self, name):
            return getattr(self.db, name)

        def create_function(self, name, arity, function, **kwargs):
            if name == "search_session_eligible":
                callbacks.append(function)
            return self.db.create_function(name, arity, function, **kwargs)

    @contextmanager
    def hooked():
        with connect() as db:
            yield Connection(db)

    monkeypatch.setattr(store, "_connect", hooked)
    assert conversation_search.search(store, "needle")["results"] == []
    assert callbacks[0].cache_info().currsize == 256
    assert callbacks[0].cache_info().misses == 600


def test_cursor_matching_and_privacy_share_snapshot_during_concurrent_write(store, monkeypatch):
    sid = store.create_session(
        title="Original title", privacy={"memoryDisabled": False, "harnessDisabled": False}
    )
    ids = messages(store, sid, 3, prefix="public")
    connect, policy = store._connect, session_settings.source_privacy
    changed = False

    def concurrent(db, identity):
        nonlocal changed
        if not changed:
            changed = True
            with connect() as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("UPDATE sessions SET title='New private title' WHERE id=?", (sid,))
                writer.execute(
                    "UPDATE session_settings SET settings=json_set(settings,'$.privacy.memoryDisabled',1) WHERE session_id=?",
                    (sid,),
                )
                writer.commit()
        return policy(db, identity)

    monkeypatch.setattr(session_settings, "source_privacy", concurrent)
    first = conversation_search.search(store, "needle", cursor=ids[-1], limit=1)
    assert [row["id"] for row in first["results"]] == [ids[-2]]
    assert first["results"][0]["title"] == "Original title" and first["next_cursor"] == ids[-2]
    assert conversation_search.search(store, "needle", cursor=ids[-1])["results"] == []


def test_direct_cursor_identity_and_query_validation_remain_compatible(store):
    sid = store.create_session(title="Public")
    ids = messages(store, sid, 3, prefix="public")
    first = conversation_search.search(store, "needle", limit=1)
    assert first["next_cursor"] == ids[-1]
    assert conversation_search.search(store, "other", cursor=first["next_cursor"])["results"] == []
    for query, limit in (("", 1), (" " * 4, 1), ("x" * 501, 1), ("x", 0), ("x", 51), ("x", True)):
        with pytest.raises(ValueError, match="Use 1-500"):
            conversation_search.search(store, query, limit=limit)
    with pytest.raises(ValueError, match="Search cursor unavailable; restart search"):
        conversation_search.search(store, "needle", cursor="missing")
    # A retained cursor is an ordering boundary, not a new authority to read its source.
    with store._connect() as db:
        db.execute(
            "INSERT INTO forgetting_receipts VALUES('del_cursor',?,0,'confirmed',0,0)", (sid,)
        )
        db.execute("INSERT INTO forgotten_sessions VALUES(?,'del_cursor')", (sid,))
    assert conversation_search.search(store, "needle", cursor=ids[-1])["results"] == []


def test_direct_and_global_routes_preserve_schemas_auth_and_coverage(store):
    public = store.create_session(title="Public")
    private = store.create_session(title="Private", privacy={"harnessDisabled": True})
    ids = messages(store, public, 207, prefix="public", created_at=0)
    messages(store, private, 300, prefix="private", created_at=1)
    app = FastAPI()
    app.include_router(conversation_search.router(lambda: store, lambda: None))
    direct = TestClient(app)
    page = direct.get("/search/conversations", params={"query": "needle", "limit": 50}).json()
    assert set(page) == {"scope", "results", "next_cursor"}
    assert [row["id"] for row in page["results"]] == list(reversed(ids))[:50]
    assert set(page["results"][0]) == {
        "id",
        "session_id",
        "seq",
        "role",
        "created_at",
        "title",
        "excerpt",
    }
    assert (
        direct.get(
            "/search/conversations", params={"query": "needle", "cursor": "missing"}
        ).status_code
        == 422
    )
    assert (
        direct.get("/search/conversations", params={"query": "needle", "limit": 51}).status_code
        == 422
    )

    def deny():
        raise HTTPException(401)

    private_app = FastAPI()
    private_app.include_router(conversation_search.router(lambda: store, deny))
    assert (
        TestClient(private_app).get("/search/conversations", params={"query": "needle"}).status_code
        == 401
    )
    universal_search.save_settings(
        store,
        universal_search.SaveSettings(
            expected_revision=0, configuration=universal_search.Settings(sources=["conversations"])
        ),
        semantic_available=False,
        ranking_available=False,
    )
    page = (
        client_for(store)
        .get("/search", params={"q": "needle", "stage": "text", "limit": 50})
        .json()
    )
    assert len(page["results"]) == 50 and page["nextCursor"]
    assert page["coverage"] == [{"source": "conversations", "status": "partial"}]
    assert all(row["recordId"] in ids and row["matchType"] == "text" for row in page["results"])
    assert page["ranking"] == {"status": "disabled"}
