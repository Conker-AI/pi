import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi import api, project_sources
from pi import projects as p
from pi.browser_contract import owner_allowed, runtime_allowed
from pi.projects_api import router
from pi.store import Store

FIELDS = dict(name="Build Conker", description="Deliver", instructions="Be critical")
PUBLIC = dict(memoryDisabled=False, harnessDisabled=False)
SESSION_ID = "ses_" + "a" * 16
REF = dict(kind="conversation", sessionId=SESSION_ID)
OWNER = "project-owner-control-key-" + "o" * 32


@pytest.fixture
def setup(tmp_path):
    with closing(Store(tmp_path / "test.db")) as store:
        yield store


def test_restart_and_revision_race(setup):
    store = setup
    record = p.create(store, p.Fields(**FIELDS))

    def write(n):
        try:
            return p.mutate(
                store,
                record["id"],
                p.Update(expected_revision=1, fields=p.Fields(**{**FIELDS, "name": str(n)})),
                "update",
            )["revision"]
        except p.ProjectError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, [1, 2]))
    assert sorted(map(str, results)) == ["2", "conflict"]
    path = store.path
    store.close()
    with closing(Store(path)) as reopened:
        assert p.get(reopened, record["id"])["revision"] == 2


def test_live_references_privacy_origin_and_deletion(setup):
    source = dict(
        originSessionId=SESSION_ID,
        label="Private source label",
        archived=False,
        privacy=PUBLIC,
    )

    def resolve(ref):
        return source

    record = p.create(setup, p.Fields(**FIELDS))
    identity = record["id"]
    body = p.Link(expected_revision=1, reference=REF)
    linked = p.mutate(setup, identity, body, "link", resolve)
    assert linked["links"][0]["label"] == source["label"]
    assert p.context(setup, identity, p.Privacy(**PUBLIC), resolve)["references"]
    source["privacy"] = {**PUBLIC, "memoryDisabled": True}
    assert (
        p.context(setup, identity, p.Privacy(**PUBLIC), resolve)["excluded"][0]["reason"]
        == "origin-private"
    )
    assert (
        p.context(setup, identity, p.Privacy(**{**PUBLIC, "incognito": True}), resolve)["excluded"][
            0
        ]["reason"]
        == "target-private"
    )
    source["originSessionId"] = "ses_" + "b" * 16
    changed = p.get(setup, identity, resolve)["links"][0]
    assert (
        changed["availability"] == "origin-changed" and changed["label"] == "Unavailable reference"
    )
    assert not p.search(setup, identity, "", resolve)["links"]
    p.mutate(setup, identity, p.Archive(expected_revision=2, archived=True), "archive", resolve)
    with pytest.raises(p.ProjectError):
        p.mutate(setup, identity, p.Revision(expected_revision=3), "remove")
    assert p.context(setup, identity, p.Privacy(**PUBLIC), resolve)["instruction"] is None
    p.mutate(setup, identity, p.Archive(expected_revision=3, archived=False), "archive")
    p.mutate(setup, identity, p.Link(expected_revision=4, reference=REF), "unlink")
    p.mutate(setup, identity, p.Archive(expected_revision=5, archived=True), "archive")
    p.mutate(setup, identity, p.Revision(expected_revision=6), "remove")
    assert p.list_projects(setup) == []
    assert source["label"] == "Private source label"


def test_unknown_source_fails_closed_and_validation(setup):
    record = p.create(setup, p.Fields(**FIELDS))
    with pytest.raises(p.ProjectError):
        p.mutate(setup, record["id"], p.Link(expected_revision=1, reference=REF), "link")
    assert p.get(setup, record["id"])["revision"] == 1
    for bad in [dict(name=" ", description="", instructions=""), {**FIELDS, "grant": "all"}]:
        with pytest.raises(ValidationError):
            p.Fields(**bad)
    with pytest.raises(ValidationError):
        p.Revision(expected_revision=True)
    with pytest.raises(ValidationError):
        p.Link(
            expected_revision=1,
            reference={
                "kind": "file",
                "sessionId": SESSION_ID,
                "fileId": "attachment_" + "b" * 32,
                "path": "/secret",
            },
        )


def test_api_is_owner_guarded(setup):
    app = FastAPI()

    def denied():
        raise HTTPException(403, "Owner only")

    app.include_router(router(lambda: setup, denied))
    with TestClient(app) as http:
        assert http.get("/projects").status_code == 403
        assert http.post("/projects", json=FIELDS).status_code == 403
    app.dependency_overrides[denied] = lambda: None
    with TestClient(app) as http:
        record = http.post("/projects", json=FIELDS).json()
        assert (
            http.post(
                f"/projects/{record['id']}/update", json={"expected_revision": 2, "fields": FIELDS}
            ).status_code
            == 409
        )
        assert (
            http.get(f"/projects/{record['id']}/search").json()["scope"] == "linked-metadata-only"
        )


def test_owner_api_contract_restart_restore_and_source_privacy(tmp_path, monkeypatch):
    path = tmp_path / "owner-projects.db"
    store = Store(path)
    source_session = store.create_session(title="Authoritative source")
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "project-admin-" + "a" * 32, raising=False)
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
        assert client.get("/projects").status_code == 401
        assert client.get("/projects", headers={"X-Pi-Gateway-Key": "r" * 32}).status_code == 401
        assert (
            client.post("/projects", headers=headers, json={**FIELDS, "secret": "x"}).status_code
            == 422
        )

        created = client.post("/projects", headers=headers, json=FIELDS)
        assert created.status_code == 200
        project = created.json()
        identity = project["id"]
        assert project["schemaVersion"] == 1
        assert project["authority"] == "none"
        assert project["contentIncluded"] is False
        assert project["grantsInherited"] is False

        malformed = client.post(
            f"/projects/{identity}/link",
            headers=headers,
            json={
                "expected_revision": 1,
                "reference": {"kind": "conversation", "sessionId": "not-a-session"},
            },
        )
        assert malformed.status_code == 422

        linked = client.post(
            f"/projects/{identity}/link",
            headers=headers,
            json={
                "expected_revision": 1,
                "reference": {"kind": "conversation", "sessionId": source_session},
            },
        )
        assert linked.status_code == 200
        assert linked.json()["links"][0]["label"] == "Authoritative source"
        assert linked.json()["links"][0]["privacy"] == {**PUBLIC, "incognito": False}

        stale = client.post(
            f"/projects/{identity}/update",
            headers=headers,
            json={"expected_revision": 1, "fields": FIELDS},
        )
        assert stale.status_code == 409
        archived = client.post(
            f"/projects/{identity}/archive",
            headers=headers,
            json={"expected_revision": 2, "archived": True},
        )
        assert archived.status_code == 200 and archived.json()["archivedAt"] is not None
        restored = client.post(
            f"/projects/{identity}/archive",
            headers=headers,
            json={"expected_revision": 3, "archived": False},
        )
        assert restored.status_code == 200 and restored.json()["archivedAt"] is None

        for suffix, body in (
            ("remove", {"expected_revision": 4}),
            ("context", PUBLIC),
        ):
            assert (
                client.post(
                    f"/projects/{identity}/{suffix}", headers=headers, json=body
                ).status_code
                == 403
            )
        assert client.get(f"/projects/{identity}/search", headers=headers).status_code == 403
        assert client.get("/projects/not-a-project", headers=headers).status_code == 403
    finally:
        store.close()

    with closing(Store(path)) as reopened:
        saved = p.get(
            reopened,
            identity,
            lambda reference: project_sources.resolve(reopened, reference),
        )
        assert saved["revision"] == 4
        assert saved["archivedAt"] is None
        assert saved["links"][0]["reference"]["sessionId"] == source_session


def test_owner_project_listing_is_bounded_and_cursor_based(tmp_path, monkeypatch):
    store = Store(tmp_path / "project-list.db")
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "project-admin-" + "a" * 32, raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    headers = {"X-Pi-Owner-Key": OWNER}
    client = TestClient(api.app)
    try:
        for index in range(3):
            assert (
                client.post(
                    "/projects", headers=headers, json={**FIELDS, "name": f"Project {index}"}
                ).status_code
                == 200
            )
        first = client.get("/projects?limit=2", headers=headers).json()
        assert first["schemaVersion"] == 1
        assert len(first["results"]) == 2
        assert first["nextCursor"] == first["results"][-1]["id"]
        second = client.get(
            "/projects", headers=headers, params={"limit": 2, "cursor": first["nextCursor"]}
        ).json()
        assert len(second["results"]) == 1 and second["nextCursor"] is None
    finally:
        store.close()


def test_browser_owner_project_allowlist_is_exact():
    identity = "project_" + "a" * 32
    for path in ("/projects", f"/projects/{identity}"):
        assert owner_allowed("GET", path)
        assert not runtime_allowed("GET", path)
    for path in (
        "/projects",
        f"/projects/{identity}/update",
        f"/projects/{identity}/archive",
        f"/projects/{identity}/link",
        f"/projects/{identity}/unlink",
    ):
        assert owner_allowed("POST", path)
        assert not runtime_allowed("POST", path)
    for method, path in (
        ("POST", f"/projects/{identity}/remove"),
        ("POST", f"/projects/{identity}/context"),
        ("GET", f"/projects/{identity}/search"),
        ("DELETE", f"/projects/{identity}"),
        ("GET", "/projects/project_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        ("GET", "/projects/project_short"),
        ("GET", f"/projects/{identity}/extra"),
    ):
        assert not owner_allowed(method, path)
