"""Temporary-database artifact durability, provenance, concurrency and inert export."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi import api
from pi import artifacts as a
from pi.artifacts_api import router
from pi.browser_contract import owner_allowed, runtime_allowed
from pi.store import Store

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
    task_id = "tsk_" + "a" * 32
    with store._connect() as db:
        db.execute(
            "INSERT INTO tasks(id,session_id,outcome,criteria,status,revision,"
            "created_at,updated_at) "
            "VALUES(?,?,'outcome','[]','planned',1,1,1)",
            (task_id, other),
        )
    with pytest.raises(a.ArtifactError, match="originating"):
        a.create(store, request.model_copy(update={"taskId": task_id}), privacy)
    created = a.create(
        store,
        a.Create(title="task", content={"kind": "markdown", "text": "owner"}, taskId=task_id),
    )
    assert created["task"] == {"taskId": task_id, "originSessionId": other}
    assert created["taskAvailability"] == "available"
    with store._connect() as db:
        db.execute("UPDATE tasks SET archived_at=1 WHERE id=?", (task_id,))
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


def test_actual_forgetting_purges_artifact_bytes_and_reopen(tmp_path):
    from pi import forgetting, session_settings

    path = tmp_path / "forget-artifacts.db"
    secret = "artifact-private-8573-source"
    derived = "artifact-private-8573-derived"
    private_title = "artifact-private-8573-title"
    value = Store(path)
    # No schema setup here: this regression exercises actual Store integration.
    session = value.create_session()
    session_settings.save(
        value,
        session,
        session_settings.Update(
            expected_revision=0,
            settings=session_settings.Settings(
                agentId="companion",
                privacy=session_settings.Privacy(memoryDisabled=True, harnessDisabled=True),
            ),
        ),
    )
    turn = value.start_turn(session)
    message = value.complete_turn(turn, secret)
    copied = a.create(
        value,
        a.FromMessage(title=private_title, sessionId=session, messageId=message["id"]),
        session_settings.source_privacy,
    )
    assert copied["privateOrigin"] is True
    a.mutate(
        value,
        copied["id"],
        a.Append(expected_revision=1, content={"kind": "markdown", "text": derived}),
        session_settings.source_privacy,
    )
    kept = owner(value)
    value.close()
    confirmation = forgetting.preview(path, session)["confirmation"]
    receipt = forgetting.forget(path, session, confirmation)
    assert receipt["session_ids"] == [session]
    reopened = Store(path)
    try:
        hidden = a.get(reopened, copied["id"], session_settings.source_privacy)
        assert hidden["availability"] == "source-redacted"
        assert hidden["versions"] == []
        assert a.get(reopened, kept["id"])["versions"] == kept["versions"]
        with pytest.raises(a.ArtifactError):
            a.export(reopened, copied["id"], resolve=session_settings.source_privacy)
        with reopened._connect() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM artifact_versions WHERE artifact_id=?", (copied["id"],)
                ).fetchone()[0]
                == 0
            )
    finally:
        reopened.close()
    for file in path.parent.glob("forget-artifacts.db*"):
        raw = file.read_bytes()
        for text in (secret, derived, private_title):
            assert text.encode() not in raw, file.name


def test_connected_owner_api_contract_restart_and_native_export(tmp_path, monkeypatch):
    path = tmp_path / "owner-artifacts.db"
    store = Store(path)
    source_session = store.create_session(title="Source")
    turn = store.start_turn(source_session)
    source_message = store.complete_turn(turn, "Exact completed response")
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "artifact-admin-" + "a" * 32, raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    monkeypatch.setattr(
        api.app.state,
        "gateway_key_hash",
        hashlib.sha256(("r" * 32).encode()).hexdigest(),
        raising=False,
    )
    headers = {"X-Pi-Owner-Key": OWNER}
    client = TestClient(api.app)
    try:
        assert client.get("/artifacts").status_code == 401
        assert client.get("/artifacts", headers={"X-Pi-Gateway-Key": "r" * 32}).status_code == 401
        assert (
            client.post(
                "/artifacts",
                headers=headers,
                json={
                    "title": "Bad",
                    "content": {"kind": "markdown", "text": "x"},
                    "credential": "must-not-be-stored",
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/artifacts/from-message",
                headers=headers,
                json={
                    "title": "Bad source",
                    "sessionId": "not-a-session",
                    "messageId": "not-a-message",
                },
            ).status_code
            == 422
        )

        copied = client.post(
            "/artifacts/from-message",
            headers=headers,
            json={
                "title": "Saved response",
                "sessionId": source_session,
                "messageId": source_message["id"],
            },
        )
        assert copied.status_code == 200
        artifact = copied.json()
        identity = artifact["id"]
        assert artifact["schemaVersion"] == 1
        assert artifact["authority"] == "none"
        assert artifact["contentIncluded"] is True
        assert artifact["execution"] == "not-wired"
        assert artifact["versions"][0]["content"]["text"] == "Exact completed response"

        listing = client.get("/artifacts?limit=1", headers=headers).json()
        assert listing["schemaVersion"] == 1
        assert listing["results"][0]["contentIncluded"] is False
        assert "versions" not in listing["results"][0]

        appended = client.post(
            f"/artifacts/{identity}/versions",
            headers=headers,
            json={
                "expected_revision": 1,
                "content": {"kind": "html", "text": "<script>never execute</script>"},
                "preserveCitations": False,
            },
        )
        assert appended.status_code == 200 and appended.json()["revision"] == 2
        stale = client.post(
            f"/artifacts/{identity}/versions",
            headers=headers,
            json={
                "expected_revision": 1,
                "content": {"kind": "markdown", "text": "stale"},
            },
        )
        assert stale.status_code == 409

        exported = client.get(f"/artifacts/{identity}/export", headers=headers)
        assert exported.status_code == 200
        assert exported.json()["schemaVersion"] == 1
        assert exported.json()["mime"] == "text/plain;charset=utf-8"
        assert exported.json()["text"] == "<script>never execute</script>"
        assert exported.json()["authority"] == "none"
        assert client.get(f"/artifacts/{identity}/download", headers=headers).status_code == 403

        archived = client.post(
            f"/artifacts/{identity}/archive",
            headers=headers,
            json={"expected_revision": 2, "archived": True},
        )
        assert archived.status_code == 200 and archived.json()["archivedAt"] is not None
        restored = client.post(
            f"/artifacts/{identity}/archive",
            headers=headers,
            json={"expected_revision": 3, "archived": False},
        )
        assert restored.status_code == 200 and restored.json()["archivedAt"] is None
        assert client.get("/artifacts/not-an-artifact", headers=headers).status_code == 403
    finally:
        store.close()

    reopened = Store(path)
    try:
        saved = a.get(reopened, identity, resolve=lambda db, sid: privacy(db, sid))
        assert saved["revision"] == 4
        assert saved["versions"][-1]["content"]["text"] == "<script>never execute</script>"
    finally:
        reopened.close()


def test_connected_artifact_listing_is_bounded_and_cursor_based(tmp_path, monkeypatch):
    store = Store(tmp_path / "artifact-list.db")
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "artifact-admin-" + "a" * 32, raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    headers = {"X-Pi-Owner-Key": OWNER}
    client = TestClient(api.app)
    try:
        for index in range(3):
            response = client.post(
                "/artifacts",
                headers=headers,
                json={
                    "title": f"Artifact {index}",
                    "content": {"kind": "markdown", "text": str(index)},
                },
            )
            assert response.status_code == 200
        first = client.get("/artifacts?limit=2", headers=headers).json()
        assert len(first["results"]) == 2
        assert first["nextCursor"] == first["results"][-1]["id"]
        second = client.get(
            "/artifacts", headers=headers, params={"limit": 2, "cursor": first["nextCursor"]}
        ).json()
        assert len(second["results"]) == 1 and second["nextCursor"] is None
    finally:
        store.close()


def test_browser_owner_artifact_allowlist_is_exact():
    identity = "artifact_" + "a" * 32
    for path in ("/artifacts", f"/artifacts/{identity}", f"/artifacts/{identity}/export"):
        assert owner_allowed("GET", path)
        assert not runtime_allowed("GET", path)
    for path in (
        "/artifacts",
        "/artifacts/from-message",
        f"/artifacts/{identity}/versions",
        f"/artifacts/{identity}/restore",
        f"/artifacts/{identity}/archive",
    ):
        assert owner_allowed("POST", path)
        assert not runtime_allowed("POST", path)
    for method, path in (
        ("GET", f"/artifacts/{identity}/download"),
        ("DELETE", f"/artifacts/{identity}"),
        ("GET", "/artifacts/artifact_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        ("GET", "/artifacts/artifact_short"),
        ("POST", f"/artifacts/{identity}/remove"),
        ("GET", f"/artifacts/{identity}/export/extra"),
    ):
        assert not owner_allowed(method, path)
