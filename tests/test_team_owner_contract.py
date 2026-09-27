"""Owner team configuration, immutable revisions and execution isolation."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi import agents, api
from pi import collaboration as c
from pi.browser_contract import owner_allowed, runtime_allowed
from pi.store import Store
from tests.test_collaboration import config, team

OWNER = "team-owner-control-" + "o" * 32


@pytest.fixture
def owner(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "teams.db")) as store:
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "recovery-" + "a" * 32, raising=False)
        monkeypatch.setattr(
            api.app.state,
            "owner_key_hash",
            hashlib.sha256(OWNER.encode()).hexdigest(),
            raising=False,
        )
        monkeypatch.setattr(
            api.app.state, "gateway_key_hash", hashlib.sha256(b"runtime").hexdigest(), raising=False
        )
        yield store, TestClient(api.app), {"X-Pi-Owner-Key": OWNER}


def test_owner_lifecycle_history_and_no_execution(owner):
    store, client, headers = owner
    agent = agents.create(store, config())
    definition = team(agent["id"]).model_dump()
    base = "/collaboration/teams"
    assert client.get(base).status_code == 401
    assert client.get(base, headers={"X-Pi-Owner-Key": OWNER + "wrong"}).status_code == 401
    assert client.get(base, headers={"X-Pi-Gateway-Key": "runtime"}).status_code == 401
    response = client.post(base, json=definition, headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    saved = response.json()
    path = base + "/" + saved["id"]
    assert saved["schemaVersion"] == 1 and saved["authority"] == "none"
    assert saved["execution"] == "configuration-only" and saved["contentIncluded"]
    assert saved["agentReferences"] == [{"roleId": "reader", "agentId": agent["id"], "revision": 1}]
    assert client.post(base, json=definition, headers=headers).status_code == 409
    changed = {**definition, "objective": "Revised objective"}
    body = {"expected_revision": 1, "definition": changed}
    assert client.post(path + "/update", json=body, headers=headers).json()["revision"] == 2
    assert client.post(path + "/update", json=body, headers=headers).status_code == 409
    archive = {"expected_revision": 2, "archived": True}
    assert client.post(path + "/archive", json=archive, headers=headers).json()["revision"] == 3
    assert (
        client.post(
            path + "/update", json={**body, "expected_revision": 3}, headers=headers
        ).status_code
        == 409
    )
    assert (
        client.post(path + "/restore", json={"expected_revision": 3}, headers=headers).json()[
            "revision"
        ]
        == 4
    )
    listing = client.get(base, headers=headers).json()
    assert listing["results"][0]["contentIncluded"] is False
    assert "definition" not in listing["results"][0]
    history = client.get(path + "/versions?limit=2", headers=headers).json()
    assert [item["revision"] for item in history["results"]] == [1, 2]
    assert history["nextRevision"] == 2
    next_page = client.get(path + "/versions?limit=2&after=2", headers=headers).json()
    assert [item["revision"] for item in next_page["results"]] == [3, 4]
    assert next_page["nextRevision"] is None
    original = client.get(path + "/versions/1", headers=headers).json()
    assert original["historical"] is True and original["definition"] == definition
    assert client.get(path + "/versions/5", headers=headers).status_code == 404
    assert client.get(base + "/team_" + "f" * 32, headers=headers).status_code == 404
    for suffix in ("prepare", "remove"):
        assert (
            client.post(
                path + "/" + suffix, json={"expected_revision": 4}, headers=headers
            ).status_code
            == 401
        )
    with store._connect() as db:
        for table in ("collaboration_preparations", "team_runs", "team_steps", "tasks", "sessions"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_invalid_references_restore_and_stored_data_fail_closed(owner):
    store, client, headers = owner
    agent = agents.create(store, config())
    definition = team(agent["id"]).model_dump()
    base = "/collaboration/teams"
    for field, value in (
        ("owner_id", "another-owner"),
        ("credentials", {}),
        ("target", {"path": "/private"}),
    ):
        assert (
            client.post(base, json={**definition, field: value}, headers=headers).status_code == 422
        )
    bad = team(agent["id"]).model_dump()
    bad["roles"][0]["agentId"] = "agent_foreign"
    assert client.post(base, json=bad, headers=headers).status_code == 422
    bad["roles"][0]["agentId"] = "agent_" + "f" * 32
    assert client.post(base, json=bad, headers=headers).status_code == 404
    saved = client.post(base, json=definition, headers=headers).json()
    path = base + "/" + saved["id"]
    agents.update(
        store,
        agent["id"],
        agents.UpdateAgent(expected_revision=1, configuration=config(toolIds=[])),
    )
    current = client.get(path, headers=headers).json()
    assert (
        current["agentReferenceState"] == "selection-conflict" and current["agentReferences"] == []
    )
    assert (
        client.post(
            path + "/update",
            json={"expected_revision": 1, "definition": definition},
            headers=headers,
        ).status_code
        == 409
    )
    assert (
        client.post(
            path + "/archive", json={"expected_revision": 1, "archived": True}, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.post(path + "/restore", json={"expected_revision": 2}, headers=headers).status_code
        == 409
    )
    assert client.get(path + "/versions/1", headers=headers).json()["definition"] == definition
    agents.archive(store, agent["id"], agents.ArchiveAgent(expected_revision=2, archived=True))
    assert client.get(path, headers=headers).json()["agentReferenceState"] == "unavailable"
    assert (
        client.post(
            path + "/archive", json={"expected_revision": 2, "archived": False}, headers=headers
        ).status_code
        == 409
    )
    with store._connect() as db:
        db.execute(
            "UPDATE collaboration_records SET definition=?,revision=revision+1 WHERE id=?",
            ('{"secret":"malformed-hidden"}', saved["id"]),
        )
    failed = client.get(path, headers=headers)
    assert failed.status_code == 503 and "malformed-hidden" not in failed.text


def test_history_atomic_under_concurrent_writers_and_sql_immutable(tmp_path):
    path = tmp_path / "teams.db"
    with closing(Store(path)) as store:
        agent = agents.create(store, config())
        definition = team(agent["id"])
        record = c.save(store, "team", definition)

        def write(index):
            try:
                return c.save(
                    store,
                    "team",
                    definition.model_copy(update={"objective": str(index)}),
                    record["id"],
                    1,
                )["revision"]
            except agents.AgentError:
                return "conflict"

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(write, range(2)))
        assert results.count(2) == results.count("conflict") == 1
        assert len(c.owner_history(store, record["id"]).results) == 2
        with store._connect() as db:
            for sql in (
                "UPDATE team_definition_revisions SET definition='{}'",
                "DELETE FROM team_definition_revisions",
                "INSERT OR REPLACE INTO team_definition_revisions SELECT * FROM team_definition_revisions",
                "INSERT OR REPLACE INTO collaboration_records SELECT * FROM collaboration_records",
            ):
                with pytest.raises(sqlite3.IntegrityError):
                    db.execute(sql)
        backup = tmp_path / "backup.db"
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as dest:
            source.backup(dest)
    for database in (path, backup):
        with closing(Store(database)) as store:
            assert c.owner_get(store, record["id"]).revision == 2
            assert c.owner_revision(store, record["id"], 1).definition == definition
            with store._connect() as db:
                assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_legacy_migration_keeps_actual_revision_without_fabricated_history(tmp_path):
    path = tmp_path / "legacy.db"
    with closing(Store(path)) as store:
        agent = agents.create(store, config())
        record = c.save(store, "team", team(agent["id"]))
        c.save(store, "team", team(agent["id"]), record["id"], 1)
        with store._connect() as db:
            db.execute("DROP TRIGGER team_definition_insert")
            db.execute("DROP TRIGGER team_definition_update")
            db.execute("DROP TABLE team_definition_revisions")
    for _ in range(2):
        with closing(Store(path)) as store:
            assert [v.revision for v in c.owner_history(store, record["id"]).results] == [2]
            with pytest.raises(agents.AgentError):
                c.owner_revision(store, record["id"], 1)


def test_exact_browser_routes_and_bounded_pagination(owner):
    store, client, headers = owner
    agent = agents.create(store, config())
    ids = []
    for index in range(3):
        ids.append(
            c.save(store, "team", team(agent["id"]).model_copy(update={"name": str(index)}))["id"]
        )
    base = "/collaboration/teams"
    first = client.get(base + "?limit=2", headers=headers).json()
    second = client.get(
        base, params={"limit": 2, "cursor": first["nextCursor"]}, headers=headers
    ).json()
    assert {row["id"] for row in first["results"] + second["results"]} == set(ids)
    assert second["nextCursor"] is None
    assert client.get(base + "?limit=101", headers=headers).status_code == 422
    path = base + "/" + ids[0]
    for method, route in [
        ("GET", base),
        ("POST", base),
        ("GET", path),
        ("GET", path + "/versions"),
        ("GET", path + "/versions/1"),
        *[("POST", path + "/" + op) for op in ("archive", "restore", "update")],
    ]:
        assert owner_allowed(method, route) and not runtime_allowed(method, route)
    for method, route in [
        ("GET", "/collaboration"),
        ("POST", path + "/prepare"),
        ("POST", path + "/remove"),
        ("POST", "/team-runs/from-team/" + ids[0]),
        ("POST", "/collaboration/templates"),
        ("GET", base + "/team_short"),
        ("GET", path + "/versions/01"),
        ("GET", path + "/versions/1/extra"),
    ]:
        assert not owner_allowed(method, route)
