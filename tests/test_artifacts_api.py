"""Artifact owner routes: auth, the connected API contract, bounded listing, browser allowlist."""

import hashlib

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from pi.api import server as api
from pi.api.browser_contract import owner_allowed, owner_download_allowed, runtime_allowed
from pi.api.routes.artifacts import router
from pi.content import artifacts as a
from pi.conversation.store import Store
from tests.artifacts_helpers import (
    OWNER,
    privacy,
    store,  # noqa: F401 -- shared fixture
)


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
        # The owner-control key downloads directly (the host CLI); the gateway's JSON relay cannot.
        downloaded = client.get(f"/artifacts/{identity}/download", headers=headers)
        assert downloaded.status_code == 200
        assert downloaded.content == b"<script>never execute</script>"
        assert downloaded.headers["content-type"].startswith("text/plain")

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
    answer = "/sessions/ses_" + "a" * 16 + "/messages/msg_" + "b" * 16 + "/download"
    for path in (f"/artifacts/{identity}/download", answer):
        assert owner_download_allowed("GET", path)
        assert not owner_download_allowed("POST", path)
        assert not owner_allowed("GET", path) and not runtime_allowed("GET", path)
    for path in (
        f"/artifacts/{identity}/download/extra",
        "/artifacts/artifact_short/download",
        "/sessions/ses_x/messages/msg_" + "b" * 16 + "/download",
        f"/artifacts/{identity}/export",
    ):
        assert not owner_download_allowed("GET", path)
