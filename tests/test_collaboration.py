"""Preparation is durable configuration, never delegation or a permission grant."""

import copy
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi import agents, api
from pi import collaboration as c
from pi.store import Store


def config(**changes):
    return agents.AgentInput.model_validate(
        {
            "name": "Reader",
            "role": "Research",
            "instructions": "Cite evidence.",
            "modelId": None,
            "toolIds": ["read", "search"],
            "memory": {"scope": "selected", "memoryIds": ["one", "two"]},
            **changes,
        }
    )


def template():
    return c.Template(name="Research template", description="A careful reader", agent=config())


def team(identity):
    budget = {"maxTurns": 2, "maxTokens": 100, "maxCostCents": 0}
    return c.Team.model_validate(
        {
            "name": "Review team",
            "objective": "Review evidence",
            "roles": [
                {
                    "id": "reader",
                    "name": "Reader",
                    "agentId": identity,
                    "instructions": "Report evidence.",
                    "toolIds": ["read"],
                    "memory": {"scope": "selected", "memoryIds": ["one"]},
                    "context": {"mode": "task_only", "sourceIds": []},
                    "budget": budget,
                }
            ],
            "handoffs": [],
            "budget": {**budget, "maxHandoffs": 0},
        }
    )


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "test.db")) as value:
        yield value


def test_durable_template_versions_and_whole_field_replacements(tmp_path):
    path = tmp_path / "test.db"
    with closing(Store(path)) as store:
        identity = c.save(store, "template", template())["id"]
        publication = c.publish(store, identity, c.Revision(expected_revision=1))
        prepared = c.prepare(
            store,
            identity,
            "template",
            c.Instantiate(
                expected_revision=2,
                version=1,
                name="New reader",
                overrides={"toolIds": [], "memory": {"scope": "none", "memoryIds": []}},
            ),
        )
        assert prepared["configuration"]["toolIds"] == []
        assert prepared["configuration"]["memory"] == {"scope": "none", "memoryIds": []}
        assert prepared["authority"] == "none" and prepared["status"] == "prepared"
        assert len(agents.list_agents(store)["results"]) == 1 and store.list_sessions() == []
        c.save(
            store, "template", template().model_copy(update={"description": "Changed"}), identity, 2
        )
        c.archive(
            store, identity, "template", agents.ArchiveAgent(expected_revision=3, archived=True)
        )
        with pytest.raises(agents.AgentError, match="Restore"):
            c.prepare(
                store,
                identity,
                "template",
                c.Instantiate(expected_revision=4, version=1, name="Other"),
            )
        c.archive(
            store, identity, "template", agents.ArchiveAgent(expected_revision=4, archived=False)
        )
        with pytest.raises(agents.AgentError, match="Archive"):
            c.remove(store, identity, "template", c.Revision(expected_revision=5))
    with closing(Store(path)) as store:
        assert c.list_all(store)["preparations"] == [prepared]
        assert c.get(store, identity, "template")["versions"] == [publication]


def test_publish_cas_and_duplicate_publication(store):
    identity = c.save(store, "template", template())["id"]

    def attempt(_):
        try:
            return c.publish(store, identity, c.Revision(expected_revision=1))
        except agents.AgentError as exc:
            return exc.detail

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert len([r for r in results if r.get("version") == 1]) == 1
    assert any(r.get("current_revision") == 2 for r in results)
    with pytest.raises(agents.AgentError, match="Change the draft"):
        c.publish(store, identity, c.Revision(expected_revision=2))


def test_team_snapshots_revalidate_agent_changes_without_widening(store):
    agent = agents.create(store, config())
    definition = team(agent["id"])
    record = c.save(store, "team", definition)
    prepared = c.prepare(store, record["id"], "team", c.Revision(expected_revision=1))
    assert prepared["agents"][0]["agentVersion"] == 1
    assert prepared["definition"]["roles"][0]["toolIds"] == ["read"]
    bad = definition.model_dump()
    bad["roles"][0]["memory"]["memoryIds"] = ["outside-selection"]
    with pytest.raises(agents.AgentError, match="memory exceeds"):
        c.save(store, "team", c.Team.model_validate(bad), record["id"], 1)
    agents.update(
        store,
        agent["id"],
        agents.UpdateAgent(expected_revision=1, configuration=config(toolIds=[])),
    )
    with pytest.raises(agents.AgentError, match="exceed"):
        c.prepare(store, record["id"], "team", c.Revision(expected_revision=1))
    agents.archive(store, agent["id"], agents.ArchiveAgent(expected_revision=2, archived=True))
    with pytest.raises(agents.AgentError, match="active"):
        c.prepare(store, record["id"], "team", c.Revision(expected_revision=1))
    assert c.list_all(store)["preparations"] == [prepared]
    with pytest.raises(agents.AgentError, match="Archive"):
        c.remove(store, record["id"], "team", c.Revision(expected_revision=1))


@pytest.mark.parametrize(
    "mutation",
    [
        "role_id",
        "role_name",
        "turns",
        "tokens",
        "cost",
        "edge_self",
        "edge_missing",
        "edge_pair",
        "transfers",
        "context",
        "strict_budget",
    ],
)
def test_team_graph_and_budget_validation(mutation):
    value = team("agent_test").model_dump()
    if mutation in ("role_id", "role_name"):
        second = copy.deepcopy(value["roles"][0])
        second["id" if mutation == "role_name" else "name"] = "other"
        value["roles"].append(second)
    elif mutation in ("turns", "tokens", "cost"):
        field = {"turns": "maxTurns", "tokens": "maxTokens", "cost": "maxCostCents"}[mutation]
        value["roles"][0]["budget"][field] = value["budget"][field] + 1
    elif mutation == "context":
        value["roles"][0]["context"]["sourceIds"] = ["source"]
    elif mutation == "strict_budget":
        value["budget"]["maxTurns"] = True
    else:
        value["roles"].append(
            {**copy.deepcopy(value["roles"][0]), "id": "writer", "name": "Writer"}
        )
        value["budget"].update(maxTurns=4, maxTokens=200, maxHandoffs=2)
        edge = {
            "id": "handoff",
            "fromRoleId": "reader",
            "toRoleId": "writer",
            "condition": "Ready",
            "payload": "result_only",
            "maxTransfers": 1,
        }
        if mutation == "edge_self":
            edge["toRoleId"] = "reader"
        if mutation == "edge_missing":
            edge["toRoleId"] = "missing"
        if mutation == "transfers":
            edge["maxTransfers"] = 3
        value["handoffs"] = [edge]
        if mutation == "edge_pair":
            value["handoffs"].append({**edge, "id": "second"})
    with pytest.raises(ValidationError):
        c.Team.model_validate(value)


def test_removal_tombstone_and_name_reuse(store):
    item = c.save(store, "template", template())
    c.remove(store, item["id"], "template", c.Revision(expected_revision=1))
    assert c.list_all(store)["templates"] == []
    with pytest.raises(agents.AgentError):
        c.get(store, item["id"], "template")
    assert c.save(store, "template", template())["id"] != item["id"]
    with store._connect() as db:
        assert db.execute(
            "SELECT deleted_at FROM collaboration_records WHERE id=?", (item["id"],)
        ).fetchone()[0]


def test_snapshots_sql_immutable(store):
    item = c.save(store, "template", template())
    c.publish(store, item["id"], c.Revision(expected_revision=1))
    c.prepare(
        store,
        item["id"],
        "template",
        c.Instantiate(expected_revision=2, version=1, name="Prepared"),
    )
    with store._connect() as db:
        for table in ("template_publications", "collaboration_preparations"):
            for sql in (
                f"DELETE FROM {table}",
                f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}",
            ):
                with pytest.raises(sqlite3.IntegrityError):
                    db.execute(sql)


def test_http_admin_and_bad_overrides(store, monkeypatch):
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "owner_admin_test_key", raising=False)
    monkeypatch.setattr(
        api.app.state,
        "gateway_key_hash",
        hashlib.sha256(b"runtime_test_key").hexdigest(),
        raising=False,
    )
    client = TestClient(api.app)
    headers = {"X-Pi-Key": "owner_admin_test_key"}
    route = "/collaboration/templates"
    assert client.get("/collaboration").status_code == 401
    assert (
        client.post(
            route, json=template().model_dump(), headers={"X-Pi-Gateway-Key": "runtime_test_key"}
        ).status_code
        == 403
    )
    item = client.post(route, json=template().model_dump(), headers=headers).json()
    path = route + "/" + item["id"]
    assert (
        client.post(path + "/publish", json={"expected_revision": 1}, headers=headers).status_code
        == 200
    )
    assert (
        client.post(
            path + "/instantiate",
            json={
                "expected_revision": 2,
                "version": 1,
                "name": "Prepared",
                "overrides": {"memory": {"scope": "none"}},
            },
            headers=headers,
        ).status_code
        == 422
    )
    assert c.list_all(store)["preparations"] == []


def test_team_bounded_cycle_update_archive_and_durable_snapshot(tmp_path):
    path = tmp_path / "test.db"
    with closing(Store(path)) as store:
        agent = agents.create(store, config())
        value = team(agent["id"]).model_dump()
        value["roles"].append(
            {**copy.deepcopy(value["roles"][0]), "id": "writer", "name": "Writer"}
        )
        value["budget"].update(maxTurns=4, maxTokens=200, maxHandoffs=2)
        value["handoffs"] = [
            {
                "id": "forward",
                "fromRoleId": "reader",
                "toRoleId": "writer",
                "condition": "Evidence ready",
                "payload": "result_and_citations",
                "maxTransfers": 1,
            },
            {
                "id": "back",
                "fromRoleId": "writer",
                "toRoleId": "reader",
                "condition": "Needs review",
                "payload": "result_only",
                "maxTransfers": 1,
            },
        ]
        record = c.save(store, "team", c.Team.model_validate(value))
        value["objective"] = "Revised objective"
        c.save(store, "team", c.Team.model_validate(value), record["id"], 1)
        with pytest.raises(agents.AgentError):
            c.prepare(store, record["id"], "team", c.Revision(expected_revision=1))
        prepared = c.prepare(store, record["id"], "team", c.Revision(expected_revision=2))
        archived = c.archive(
            store, record["id"], "team", agents.ArchiveAgent(expected_revision=2, archived=True)
        )
        assert archived["revision"] == 3
        with pytest.raises(agents.AgentError):
            c.prepare(store, record["id"], "team", c.Revision(expected_revision=3))
    with closing(Store(path)) as store:
        assert c.list_all(store)["preparations"] == [prepared]
        assert c.get(store, record["id"], "team")["archived_at"] is not None


def test_unused_team_can_be_removed_without_removing_agent(store):
    agent = agents.create(store, config())
    record = c.save(store, "team", team(agent["id"]))
    c.remove(store, record["id"], "team", c.Revision(expected_revision=1))
    assert c.list_all(store)["teams"] == []
    assert agents.get(store, agent["id"]) == agent
