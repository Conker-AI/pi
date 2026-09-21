"""Temporary-database artifact durability, provenance, concurrency and inert export."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi import artifacts as a
from pi.artifacts_api import router
from pi.store import Store


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


def test_persistence_restore_and_immutable_history(store):
    initial = owner(store)
    identity = initial["id"]
    updated = a.mutate(
        store,
        identity,
        a.Append(
            expected_revision=1,
            title="Second",
            content={"kind": "code", "language": "py", "text": "print('inert')"},
        ),
    )
    restored = a.mutate(store, identity, a.Restore(expected_revision=2, version=1))
    assert restored["versions"][0] == initial["versions"][0]
    assert restored["versions"][1] == updated["versions"][1]
    assert restored["versions"][2]["restoredFromVersion"] == 1
    path = store.path
    store.close()
    reopened = Store(path)
    assert a.get(reopened, identity) == restored
    with reopened._connect() as db:
        for sql in (
            "UPDATE artifact_versions SET body='{}'",
            "DELETE FROM artifact_versions",
            "INSERT OR REPLACE INTO artifact_versions SELECT * FROM artifact_versions",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)
    reopened.close()


def test_competing_mutations_and_archive_cas(store):
    identity = owner(store)["id"]
    barrier = Barrier(2)

    def update(text):
        barrier.wait()
        try:
            return a.mutate(
                store,
                identity,
                a.Append(expected_revision=1, content={"kind": "markdown", "text": text}),
            )["revision"]
        except a.ArtifactError as exc:
            return exc.detail["code"]

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(update, ["one", "two"]))
    assert sorted(map(str, outcomes)) == ["2", "revision_conflict"]
    archived = a.mutate(store, identity, a.Archive(expected_revision=2, archived=True))
    assert archived["revision"] == 3
    with pytest.raises(a.ArtifactError, match="Restore"):
        a.mutate(store, identity, a.Restore(expected_revision=3, version=1))
    with pytest.raises(a.ArtifactError, match="changed"):
        a.mutate(store, identity, a.Archive(expected_revision=2, archived=False))
    assert (
        a.mutate(store, identity, a.Archive(expected_revision=3, archived=False))["revision"] == 4
    )


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "code", "text": "x", "language": "py", "execute": True},
        {"kind": "table", "columns": ["a"], "rows": [["a", "b"]]},
        {
            "kind": "chart",
            "chartType": "line",
            "xLabel": "x",
            "series": [{"label": "a"}],
            "rows": [{"label": "x", "values": [float("nan")]}],
        },
        {
            "kind": "chart",
            "chartType": "bar",
            "xLabel": "x",
            "series": [{"label": "a"}, {"label": "a"}],
            "rows": [],
        },
        {
            "kind": "diagram",
            "nodes": [],
            "edges": [{"id": "e", "source": "missing", "target": "also-missing"}],
        },
        {
            "kind": "media",
            "mediaType": "image",
            "url": "https://name:secret@example.org/a",
            "description": "",
        },
        {"kind": "media", "mediaType": "image", "url": "javascript:alert(1)", "description": ""},
        {"kind": "markdown", "text": "x" * 200001},
    ],
)
def test_invalid_content(data):
    with pytest.raises((ValueError, ValidationError)):
        a.content(data)


def test_source_privacy_is_live_and_never_caller_supplied(store):
    request = response(store)
    with pytest.raises(a.ArtifactError, match="authoritative privacy"):
        a.create(store, request)
    copied = a.create(store, request, privacy)
    assert copied["privateOrigin"] is True
    identity = copied["id"]
    assert a.export(store, identity, resolve=privacy)["text"] == "private source"
    hidden = a.get(store, identity)
    assert hidden["availability"] == "privacy-unknown"
    assert hidden["title"] == "Unavailable artifact" and hidden["versions"] == []
    assert hidden["privacy"] is None and hidden["privateOrigin"] is None
    with pytest.raises(a.ArtifactError):
        a.export(store, identity)
    with pytest.raises(ValidationError):
        a.FromMessage.model_validate({**request.model_dump(), "privacy": {"memoryDisabled": False}})
    with store._connect() as db:
        db.execute("UPDATE turns SET status='failed' WHERE session_id=?", (request.sessionId,))
    assert a.get(store, identity, privacy)["availability"] == "source-changed"


def test_legacy_or_incomplete_messages_cannot_be_copied(store):
    session = store.create_session()
    message = store.append_message(session, "assistant", "legacy text")
    with pytest.raises(a.ArtifactError):
        a.create(
            store, a.FromMessage(title="x", sessionId=session, messageId=message["id"]), privacy
        )


def test_archive_source_and_purge_all_derived_versions(store):
    request = response(store)
    created = a.create(store, request, privacy)
    identity = created["id"]
    a.mutate(
        store,
        identity,
        a.Append(expected_revision=1, content={"kind": "markdown", "text": "derived private"}),
        privacy,
    )
    with store._connect() as db:
        db.execute("UPDATE sessions SET status='closed' WHERE id=?", (request.sessionId,))
    assert a.get(store, identity, privacy)["availability"] == "source-archived"
    assert a.export(store, identity, resolve=privacy)["text"] == "derived private"
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        a.redact(db, [request.sessionId])
        db.commit()
        assert not db.execute(
            "SELECT * FROM artifact_versions WHERE artifact_id=?", (identity,)
        ).fetchall()
        row = db.execute(
            "SELECT title,source_text FROM artifacts WHERE id=?", (identity,)
        ).fetchone()
        assert tuple(row) == ("Unavailable artifact", None)
    assert a.get(store, identity, privacy)["versions"] == []
    with pytest.raises(a.ArtifactError):
        a.export(store, identity, resolve=privacy)


def test_csv_formula_safety_and_inert_exports(store):
    data = {
        "kind": "table",
        "columns": ["=header"],
        "rows": [[" \x01=evil"], ["\ttext"], ['safe,"text']],
    }
    created = owner(store, data)
    exported = a.export(store, created["id"])
    assert exported["text"].startswith('"\'=header"\r\n"\' \x01=evil"')
    assert exported["filename"] == "artifact-CON-v1.csv"
    html = owner(store, {"kind": "html", "text": "<script>evil()</script>"})
    exported = a.export(store, html["id"])
    assert exported["mime"] == "text/plain;charset=utf-8"
    assert exported["filename"].endswith(".html.txt")
    assert exported["execution"] == "not-wired"


def test_router_owner_auth_and_validation(store):
    def authorize(key: str | None = Header(default=None)):
        if key != "owner":
            raise HTTPException(403)

    app = FastAPI()
    app.include_router(router(lambda: store, authorize))
    client = TestClient(app)
    assert client.get("/artifacts").status_code == 403
    assert (
        client.post(
            "/artifacts",
            headers={"key": "owner"},
            json={"title": "x", "content": {"kind": "markdown", "text": "x"}, "source": {}},
        ).status_code
        == 422
    )
    created = client.post(
        "/artifacts",
        headers={"key": "owner"},
        json={"title": "x", "content": {"kind": "markdown", "text": "x"}},
    )
    assert created.status_code == 200
    identity = created.json()["id"]
    assert (
        client.post(
            f"/artifacts/{identity}/archive",
            headers={"key": "owner"},
            json={"archived": True, "expected_revision": True},
        ).status_code
        == 422
    )


def test_task_origin_and_live_availability(store):
    request = response(store)
    other = store.create_session()
    with store._connect() as db:
        db.execute(
            "INSERT INTO tasks(id,session_id,outcome,criteria,status,revision,"
            "created_at,updated_at) "
            "VALUES('task_test',?,'outcome','[]','planned',1,1,1)",
            (other,),
        )
    with pytest.raises(a.ArtifactError, match="originating"):
        a.create(store, request.model_copy(update={"taskId": "task_test"}), privacy)
    created = a.create(
        store,
        a.Create(title="task", content={"kind": "markdown", "text": "owner"}, taskId="task_test"),
    )
    assert created["task"] == {"taskId": "task_test", "originSessionId": other}
    assert created["taskAvailability"] == "available"
    with store._connect() as db:
        db.execute("UPDATE tasks SET archived_at=1 WHERE id='task_test'")
    assert a.get(store, created["id"])["taskAvailability"] == "archived"


def test_content_size_and_numeric_strictness():
    with pytest.raises(ValueError, match="250,000"):
        a.content({"kind": "table", "columns": ["a"], "rows": [["x" * 10000]] * 26})
    with pytest.raises(ValidationError):
        a.content(
            {
                "kind": "diagram",
                "nodes": [{"id": "a", "label": "A", "x": True, "y": 0}],
                "edges": [],
            }
        )


def test_missing_source_and_resolver_failure_hide_every_version(store):
    request = response(store)
    identity = a.create(store, request, privacy)["id"]

    def failure(db, session_id):
        raise RuntimeError("private detail")

    assert a.get(store, identity, failure)["availability"] == "privacy-unknown"
    with store._connect() as db:
        db.execute("UPDATE artifacts SET source_message_id='missing' WHERE id=?", (identity,))
    view = a.get(store, identity, privacy)
    assert view["availability"] == "source-unavailable" and view["versions"] == []
    assert "private source" not in str(view) and "private title" not in str(view)
