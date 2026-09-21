from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi import projects as p
from pi.projects_api import router
from pi.store import Store

FIELDS = dict(name="Build Conker", description="Deliver", instructions="Be critical")
PUBLIC = dict(memoryDisabled=False, harnessDisabled=False)
REF = dict(kind="conversation", sessionId="session-a")


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
        originSessionId="session-a", label="Private source label", archived=False, privacy=PUBLIC
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
    source["originSessionId"] = "moved"
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
            reference={"kind": "file", "sessionId": "x", "fileId": "y", "path": "/secret"},
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
