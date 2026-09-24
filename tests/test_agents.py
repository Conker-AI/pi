"""Authored profiles retain history without creating runtime authority."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi import agents, api
from pi.store import Store


def configuration(**changes):
    return agents.AgentInput.model_validate(
        {
            "name": "Researcher",
            "role": "Review evidence",
            "instructions": "Cite evidence and report uncertainty.",
            "modelId": None,
            "toolIds": [],
            "memory": {"scope": "conversation", "memoryIds": []},
            **changes,
        }
    )


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "test.db")) as value:
        yield value


def test_durability_history_archive_and_restore(tmp_path):
    path = tmp_path / "test.db"
    with closing(Store(path)) as store:
        original = agents.create(store, configuration())
        identity = original["id"]
        edited = agents.update(
            store,
            identity,
            agents.UpdateAgent(
                expected_revision=1, configuration=configuration(name="Evidence reader")
            ),
        )
        archived = agents.archive(
            store, identity, agents.ArchiveAgent(expected_revision=2, archived=True)
        )
        assert archived["revision"] == 3 and archived["archived_at"] is not None
        assert agents.get(store, identity, 1) == original
        assert agents.get(store, identity, 2) == edited
        assert len(agents.list_agents(store)["results"]) == 2
        assert store.list_sessions() == []
        with pytest.raises(agents.AgentError, match="Restore"):
            agents.update(
                store,
                identity,
                agents.UpdateAgent(expected_revision=3, configuration=configuration()),
            )
        assert (
            agents.archive(store, identity, agents.ArchiveAgent(expected_revision=3, archived=True))
            == archived
        )
        restored = agents.archive(
            store, identity, agents.ArchiveAgent(expected_revision=3, archived=False)
        )
        assert restored["revision"] == 4 and restored["archived_at"] is None
        history = agents.history(store, identity)
    with closing(Store(path)) as store:
        assert agents.get(store, identity) == restored
        assert agents.history(store, identity) == history
        assert [item["change_kind"] for item in history["results"]] == [
            "created",
            "updated",
            "archived",
            "restored",
        ]
        assert (
            len(
                [
                    item
                    for item in agents.list_agents(store)["results"]
                    if item["kind"] == "companion"
                ]
            )
            == 1
        )


def test_cas_race_and_atomic_name_collision(store):
    first = agents.create(store, configuration())
    agents.create(store, configuration(name="Other"))

    def attempt(name):
        try:
            return agents.update(
                store,
                first["id"],
                agents.UpdateAgent(expected_revision=1, configuration=configuration(name=name)),
            )
        except agents.AgentError as exc:
            return exc.detail

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["A", "B"]))
    assert len([result for result in results if result.get("revision") == 2]) == 1
    assert next(result for result in results if "code" in result)["current_revision"] == 2
    with pytest.raises(agents.AgentError, match="unique"):
        agents.update(
            store,
            first["id"],
            agents.UpdateAgent(expected_revision=2, configuration=configuration(name="OTHER")),
        )
    assert len(agents.history(store, first["id"])["results"]) == 2
    with pytest.raises(agents.AgentError) as failure:
        agents.archive(store, first["id"], agents.ArchiveAgent(expected_revision=1, archived=True))
    assert failure.value.detail["code"] == "revision_conflict"


def test_singular_companion_and_archived_name_protection(store):
    for action in (
        lambda: agents.create(store, configuration(name="CONKER")),
        lambda: agents.update(
            store,
            "companion",
            agents.UpdateAgent(expected_revision=1, configuration=configuration()),
        ),
        lambda: agents.archive(
            store, "companion", agents.ArchiveAgent(expected_revision=1, archived=True)
        ),
    ):
        with pytest.raises(agents.AgentError):
            action()
    item = agents.create(store, configuration())
    agents.archive(store, item["id"], agents.ArchiveAgent(expected_revision=1, archived=True))
    with pytest.raises(agents.AgentError):
        agents.create(store, configuration())
    for identity, revision in [("missing", None), (item["id"], 99)]:
        with pytest.raises(agents.AgentError) as failure:
            agents.get(store, identity, revision)
        assert failure.value.status == 404


@pytest.mark.parametrize(
    "changes",
    [
        {"name": " "},
        {"name": "a" * 81},
        {"role": "a" * 161},
        {"instructions": "a" * 8001},
        {"modelId": ""},
        {"modelId": 1},
        {"toolIds": ["x", "x"]},
        {"toolIds": [" "]},
        {"toolIds": [1]},
        {"memory": {"scope": "selected", "memoryIds": []}},
        {"memory": {"scope": "none", "memoryIds": ["x"]}},
        {"memory": {"scope": "all", "memoryIds": []}},
        {"kind": "companion"},
        {"grants": ["admin"]},
    ],
)
def test_invalid_configuration(changes):
    with pytest.raises(ValidationError):
        configuration(**changes)


def test_selections_are_not_authority_and_snapshots_cannot_be_rewritten(store):
    item = agents.create(
        store,
        configuration(
            modelId="requested-model",
            toolIds=["requested-tool"],
            memory={"scope": "selected", "memoryIds": ["requested-memory"]},
        ),
    )
    assert item["authority"] == "none" and item["reference_validation"] == "not-performed"
    assert item["execution"] == "not-integrated"
    statements = [
        ("UPDATE agent_versions SET configuration='{}' WHERE agent_id=?", (item["id"],)),
        ("DELETE FROM agent_versions WHERE agent_id=?", (item["id"],)),
        ("DELETE FROM agents WHERE id=?", (item["id"],)),
        ("UPDATE agents SET kind='companion' WHERE id=?", (item["id"],)),
        (
            "INSERT OR REPLACE INTO agent_versions SELECT * FROM agent_versions WHERE agent_id=?",
            (item["id"],),
        ),
    ]
    with store._connect() as db:
        for sql, args in statements:
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql, args)
    assert agents.get(store, item["id"]) == item


def test_http_admin_only_and_validation(store, monkeypatch):
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
    item = client.post("/agents", headers=headers, json=configuration().model_dump()).json()
    identity = item["id"]
    requests = [
        ("get", "/agents", None),
        ("post", "/agents", configuration().model_dump()),
        ("get", f"/agents/{identity}", None),
        ("get", f"/agents/{identity}/versions", None),
        ("get", f"/agents/{identity}/versions/1", None),
        (
            "post",
            f"/agents/{identity}/update",
            {"expected_revision": 1, "configuration": configuration().model_dump()},
        ),
        ("post", f"/agents/{identity}/archive", {"expected_revision": 1, "archived": True}),
    ]
    for method, path, body in requests:
        args = {"json": body} if body is not None else {}
        assert getattr(client, method)(path, **args).status_code == 401
        assert (
            getattr(client, method)(
                path, headers={"X-Pi-Gateway-Key": "runtime_test_key"}, **args
            ).status_code
            == 403
        )
    assert client.get(f"/agents/{identity}", headers=headers).json() == item
    assert client.get(f"/agents/{identity}/versions/1", headers=headers).json() == item
    assert (
        client.post(
            f"/agents/{identity}/archive",
            headers=headers,
            json={"expected_revision": True, "archived": True},
        ).status_code
        == 422
    )
    assert client.get("/agents/missing", headers=headers).status_code == 404
