"""Privacy exclusions survive durable queues, retries and forks."""

import hashlib
import json
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient

from pi import (
    agents,
    api,
    context_controls,
    memory_store,
    setup_choices,
    submissions,
)
from pi import (
    session_settings as settings,
)
from pi.loop import Loop, TurnFailed
from pi.memory import Memory, MemoryClient
from pi.providers import Completion
from pi.routing import Router
from pi.store import Store
from pi.toolgate import Tool


class Provider:
    name = "test"

    def __init__(self):
        self.calls = []

    def complete(self, messages, *, model):
        self.calls.append((messages, model))
        return Completion(text="Answer", provider=self.name, model=model)


def change(store, sid, memory=False, harness=False, agent="companion"):
    return settings.save(
        store,
        sid,
        settings.Update(
            expected_revision=settings.load(store, sid)["revision"],
            settings=settings.Settings(
                agentId=agent,
                privacy=settings.Privacy(memoryDisabled=memory, harnessDisabled=harness),
            ),
        ),
    )


def config(**overrides):
    return agents.AgentInput.model_validate(
        {
            "name": "Reader",
            "role": "Research",
            "instructions": "Use only cited evidence.",
            "modelId": None,
            "toolIds": ["read"],
            "memory": {"scope": "none", "memoryIds": []},
            **overrides,
        }
    )


def client(handler):
    return MemoryClient(
        "http://memory.test", "ingest_test", "read_test", transport=httpx.MockTransport(handler)
    )


@pytest.mark.parametrize(
    "scope,private,expected",
    [
        ("conversation", False, "conversation"),
        ("owner", False, "all"),
        ("owner", True, None),
    ],
)
def test_saved_companion_scope_controls_new_chat_retrieval(tmp_path, scope, private, expected):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(
            200,
            json={
                "scope": body.get("scope", "all"),
                "memories": [],
                "retrieval": {"mode": "semantic", "semantic": {"status": "ok"}},
            },
        )

    with closing(Store(tmp_path / "test.db")) as store, closing(client(handler)) as remote:
        companion = agents.get(store, "companion")
        profile = agents.AgentInput.model_validate(companion["configuration"])
        profile.memory = agents.MemorySelection(scope=scope, memoryIds=[])
        agents.update(
            store,
            "companion",
            agents.UpdateAgent(
                expected_revision=companion["revision"],
                configuration=profile,
            ),
        )
        sid = store.create_session(agent_id="companion", privacy={"memoryDisabled": private})
        result = Loop(
            store,
            Router(local_provider=Provider(), local_model="fixed"),
            memory=Memory(store, remote),
        ).run_turn(sid, "Plan my week")
        assert settings.load(store, sid)["revision"] == 1
        if expected is None:
            assert seen == []
            assert result["memory"]["retrieval"]["status"] == "disabled"
        else:
            assert len(seen) == 1
            assert seen[0].get("scope", "all") == expected
            assert remote.read_headers["X-Agent-Id"] == "default"
            if expected == "conversation":
                assert seen[0]["session_id"] == sid


def test_authored_companion_profile_applies_even_to_legacy_chat(tmp_path):
    seen = []
    with closing(Store(tmp_path / "test.db")) as store:
        sid = store.create_session()
        profile = agents.AgentInput.model_validate(agents.get(store, "companion")["configuration"])
        agents.update(
            store, "companion", agents.UpdateAgent(expected_revision=1, configuration=profile)
        )

        def handler(request):
            body = json.loads(request.content)
            seen.append(body)
            return httpx.Response(
                200,
                json={
                    "scope": "conversation",
                    "memories": [],
                    "retrieval": {"mode": "explicit-scope", "semantic": {"status": "not-used"}},
                },
            )

        with closing(client(handler)) as remote:
            Loop(
                store,
                Router(local_provider=Provider(), local_model="fixed"),
                memory=Memory(store, remote),
            ).run_turn(sid, "Use my configured scope")
        assert seen[0]["scope"] == "conversation"
        assert seen[0]["session_id"] == sid


def test_setup_memory_choice_becomes_a_stable_new_conversation_default(tmp_path):
    with closing(Store(tmp_path / "test.db")) as store:
        setup_choices.record(
            store,
            "memory",
            setup_choices.ChoiceInput(
                requestId="setup-memory-default-skip",
                choice="skip",
                expectedRevision=0,
            ),
        )
        private = store.create_session()
        assert settings.load(store, private)["settings"]["privacy"]["memoryDisabled"] is True

        setup_choices.record(
            store,
            "memory",
            setup_choices.ChoiceInput(
                requestId="setup-memory-default-include",
                choice="include",
                expectedRevision=1,
            ),
        )
        enabled = store.create_session()
        assert settings.load(store, enabled)["settings"]["privacy"]["memoryDisabled"] is False
        assert settings.load(store, private)["settings"]["privacy"]["memoryDisabled"] is True


def test_new_conversation_selects_an_active_agent_atomically(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "test.db")) as store:
        setup_choices.record(
            store,
            "memory",
            setup_choices.ChoiceInput(
                requestId="agent-memory-default-skip",
                choice="skip",
                expectedRevision=0,
            ),
        )
        agent = agents.create(store, config())
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        selected = api.create_session(api.NewSession(title="Research", agent_id=agent["id"]))[
            "session_id"
        ]
        saved = settings.load(store, selected)
        assert saved["revision"] == 1
        assert saved["settings"] == {
            "agentId": agent["id"],
            "privacy": {"memoryDisabled": True, "harnessDisabled": False},
        }
        turn = store.start_turn(selected)
        store.append_message(selected, "assistant", "Attributed", turn_id=turn, purpose="final")
        assert store.messages(selected)[0]["agent_id"] == agent["id"]

        restricted = api.create_session(
            api.NewSession(
                title="Private",
                agent_id="companion",
                privacy={"memoryDisabled": False, "harnessDisabled": True},
            )
        )["session_id"]
        assert settings.load(store, restricted)["settings"]["privacy"] == {
            "memoryDisabled": False,
            "harnessDisabled": True,
        }

        agents.archive(
            store,
            agent["id"],
            agents.ArchiveAgent(expected_revision=1, archived=True),
        )
        before = len(store.list_sessions())
        with pytest.raises(agents.AgentError, match="active agent"):
            store.create_session(title="Must not exist", agent_id=agent["id"])
        assert len(store.list_sessions()) == before


def test_future_privacy_retains_prior_queue_and_never_backfills_private_text(tmp_path):
    path = tmp_path / "test.db"
    with closing(Store(path)) as store:
        sid = store.create_session()
        prior = store.append_message(sid, "user", "Previously allowed")
        change(store, sid, memory=True)
        private = store.append_message(sid, "user", "Private future message")
        with store._connect() as db:
            assert [row[0] for row in db.execute("SELECT message_id FROM memory_outbox")] == [
                prior["id"]
            ]
        change(store, sid)
        with store._connect() as db:
            assert settings.source_privacy(db, sid)["memoryDisabled"] is True
    with closing(Store(path)) as store:
        with store._connect() as db:
            assert not db.execute(
                "SELECT 1 FROM memory_outbox WHERE message_id=?", (private["id"],)
            ).fetchone()
            # Even an erroneous queue insertion cannot bypass the delivery gate.
            db.execute(
                "INSERT INTO memory_outbox(message_id,operation) VALUES (?,'ingest')",
                (private["id"],),
            )
        with pytest.raises(ValueError, match="excluded"):
            memory_store.pin_destination(store, private["id"], "default")


def test_no_memory_turn_skips_remote_retrieval_and_delivery(tmp_path):
    with closing(Store(tmp_path / "test.db")) as store:
        sid = store.create_session()
        change(store, sid, memory=True)
        calls = []
        remote = client(lambda request: calls.append(request) or httpx.Response(500))
        memory = Memory(store, remote)
        provider = Provider()
        result = Loop(
            store, Router(local_provider=provider, local_model="fixed"), memory=memory
        ).run_turn(sid, "Private")
        assert result["memory"]["retrieval"]["status"] == "disabled"
        assert memory.drain_once() == 0 and calls == []
        assert result["memory"]["pending_ingestion"] == 0
        with store._connect() as db:
            message_id = db.execute("SELECT id FROM messages WHERE role='user'").fetchone()[0]
            db.execute(
                "INSERT INTO memory_outbox(message_id,operation) VALUES (?,'ingest')", (message_id,)
            )
        assert memory.drain_once() == 0 and calls == []
        assert memory.status(sid)["blocked_delivery"] == 1
        remote.close()


def test_settings_freeze_at_reservation_and_block_changes(tmp_path):
    with closing(Store(tmp_path / "test.db")) as store:
        sid = store.create_session()
        agent = agents.create(store, config())
        change(store, sid, memory=True, agent=agent["id"])
        request = "request_snapshot_0001"
        submissions.reserve(store, request, sid, "Input", {})
        with pytest.raises(agents.AgentError, match="Resolve"):
            change(store, sid)
        agents.update(
            store,
            agent["id"],
            agents.UpdateAgent(
                expected_revision=1, configuration=config(instructions="Later edit")
            ),
        )
        receipt = submissions.bind(store, request)
        frozen = settings.execution(store, sid, receipt["turn_id"])
        assert (
            frozen["agentVersion"] == 1
            and frozen["configuration"]["instructions"] == "Use only cited evidence."
        )
        with pytest.raises(agents.AgentError, match="Resolve"):
            change(store, sid)
        store.finish_turn(receipt["turn_id"], "failed")
        change(store, sid)
        assert settings.execution(store, sid, receipt["turn_id"]) == frozen


def test_fork_inherits_settings_and_harness_preserves_tools(tmp_path):
    with closing(Store(tmp_path / "test.db")) as store:
        sid = store.create_session()
        change(store, sid, memory=True, harness=True)
        child = store.create_session(parent_id=sid)
        assert settings.load(store, child)["settings"] == settings.load(store, sid)["settings"]
        provider = Provider()
        router = Router(local_provider=provider, local_model="fixed")

        class Tools:
            def tools(self):
                return [Tool("read", "Read", "Read a selected source", [])]

        loop = Loop(store, router, toolgate=Tools())
        loop._summarise = lambda *_: pytest.fail("No-harness must not invoke a summary helper")
        result = loop.run_turn(sid, "Question")
        assert result["message"]["content"] == "Answer"
        assert any("read" in message.content for message in provider.calls[0][0])
        with pytest.raises(TurnFailed, match="No harness"):
            loop.fork(sid)
        loop.fork_threshold_chars = 1
        with pytest.raises(TurnFailed, match="No harness"):
            loop.run_turn(child, "Would require summary")
        assert len(provider.calls) == 1


def test_agent_instructions_tool_intersection_and_unmapped_model_fail_closed(tmp_path):
    with closing(Store(tmp_path / "test.db")) as store:
        sid = store.create_session()
        agent = agents.create(store, config())
        change(store, sid, agent=agent["id"])
        provider = Provider()

        class Tools:
            def tools(self):
                return [
                    Tool("read", "Read", "Allowed", []),
                    Tool("write", "Write", "Unselected", []),
                ]

        loop = Loop(store, Router(local_provider=provider, local_model="fixed"), toolgate=Tools())
        execution = settings.execution(store, sid)
        assert [t.id for t in loop._available_tools(execution)] == ["read"]
        result = loop.run_turn(sid, "Read")
        assert any(m.content == "Use only cited evidence." for m in provider.calls[0][0])
        assert result["memory"]["retrieval"]["status"] == "not_configured"
        agents.update(
            store,
            agent["id"],
            agents.UpdateAgent(expected_revision=1, configuration=config(modelId="unmapped")),
        )
        with pytest.raises(TurnFailed, match="mapping"):
            loop.run_turn(sid, "Do not silently switch")
        assert len(provider.calls) == 1


def test_scoped_memory_rejects_older_response_without_scope_marker():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"memories": [], "retrieval": {}})

    remote = client(handler)
    with pytest.raises(ValueError, match="enforce"):
        remote.retrieve("query", scope="selected", memory_ids=["memory-one"])
    assert seen[0]["scope"] == "selected" and seen[0]["memory_ids"] == ["memory-one"]
    remote.close()


def test_admin_settings_validation_cas_and_source_unavailable(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "test.db")) as store:
        sid = store.create_session()
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "owner_admin_test_key", raising=False)
        monkeypatch.setattr(
            api.app.state,
            "gateway_key_hash",
            hashlib.sha256(b"runtime_test_key").hexdigest(),
            raising=False,
        )
        client = TestClient(api.app)
        route, headers = f"/sessions/{sid}/settings", {"X-Pi-Key": "owner_admin_test_key"}
        body = {
            "expected_revision": 0,
            "settings": {
                "agentId": "companion",
                "privacy": {"memoryDisabled": True, "harnessDisabled": False},
            },
        }
        assert client.get(route).status_code == 401
        assert (
            client.post(
                route, json=body, headers={"X-Pi-Gateway-Key": "runtime_test_key"}
            ).status_code
            == 401
        )
        monkeypatch.setattr(
            api.app.state,
            "owner_key_hash",
            hashlib.sha256(b"owner_control_test_key").hexdigest(),
            raising=False,
        )
        owner_headers = {"X-Pi-Owner-Key": "owner_control_test_key"}
        assert client.get(route, headers=owner_headers).json()["revision"] == 0
        assert client.post(route, json=body, headers=owner_headers).status_code == 200
        assert client.post(route, json=body, headers=headers).status_code == 409
        body["settings"]["privacy"]["memoryDisabled"] = "true"
        assert client.post(route, json=body, headers=headers).status_code == 422
        with store._connect() as db:
            assert settings.source_privacy(db, "missing") is None


def test_reviewed_fork_inherits_private_settings(tmp_path):
    with closing(Store(tmp_path / "test.db")) as store:
        sid = store.create_session()
        change(store, sid, memory=True, harness=True)
        policy = context_controls.Policy(
            sessionInstructions="Retain this instruction",
            messagePolicies={},
            budget=context_controls.Budget(
                contextWindowTokens=1000, outputReserveTokens=100, otherInputTokens=0
            ),
        )
        context_controls.save(
            store, sid, context_controls.Update(expected_revision=0, policy=policy)
        )
        fork = context_controls.reviewed_fork(
            store,
            sid,
            context_controls.ReviewedFork(
                expected_revision=1,
                expected_last_message_id=None,
                summary="Owner reviewed context",
                request_id="reviewed_privacy_fork_01",
            ),
        )
        assert (
            settings.load(store, fork["session_id"])["settings"]
            == settings.load(store, sid)["settings"]
        )


def test_privacy_does_not_force_local_answer_provider(tmp_path):
    from pi.routing import Reason, Route, Tier

    with closing(Store(tmp_path / "test.db")) as store:
        sid = store.create_session()
        change(store, sid, memory=True, harness=True)
        provider = Provider()

        class HostedRoute:
            def adapters(self):
                return {"test": provider}

            def candidates(self, ctx):
                return [Route(Tier.STRONG, Reason.OWNER_ASKED, "test", "frontier-answer")]

            def provider_for(self, route):
                return provider

        result = Loop(store, HostedRoute()).run_turn(sid, "Answer remotely")
        assert result["message"]["content"] == "Answer"
        assert provider.calls[0][1] == "frontier-answer"
