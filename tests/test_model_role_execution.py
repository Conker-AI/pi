"""Recorded provider dispatch uses the submitted configuration, not later edits."""

import json
from contextlib import closing

import pytest

from pi import agents, model_roles, session_settings, submissions
from pi.loop import Loop, TurnFailed
from pi.providers import Completion, ProviderUnavailable
from pi.routing import Router
from pi.store import Store


class Adapter:
    def __init__(self, name, response="Answer", fail=False):
        self.name, self.response, self.fail, self.calls = name, response, fail, []

    def complete_bounded(self, messages, *, model, timeout):
        self.calls.append((messages, model, timeout))
        if self.fail:
            raise ProviderUnavailable("Unavailable")
        return Completion(text=self.response, model=model, provider=self.name)

    def complete(self, messages, *, model):
        self.calls.append((messages, model, None))
        return Completion(text=self.response, model=model, provider=self.name)


def config(primary="a", mode="manual"):
    empty = dict(
        enabled=False,
        eligibleModelIds=[],
        modelId=None,
        timeoutMs=1700,
        failure="stop",
        fallbackModelId=None,
    )
    answer = {**empty, "enabled": True, "eligibleModelIds": ["a", "b"], "modelId": primary}
    return model_roles.Configuration.model_validate(
        {
            "providers": [
                {"id": "local", "name": "Local", "enabled": True},
                {"id": "hosted", "name": "Hosted", "enabled": True},
            ],
            "models": [
                {
                    "id": "a",
                    "providerId": "local",
                    "name": "A",
                    "route": "actual-a",
                    "enabled": True,
                },
                {
                    "id": "b",
                    "providerId": "hosted",
                    "name": "B",
                    "route": "actual-b",
                    "enabled": True,
                },
            ],
            "defaultModelId": "a",
            "roleSettings": {
                "answerMode": mode,
                "roles": {
                    **{role: dict(empty) for role in model_roles.ROLES},
                    "answer": answer,
                    "routing": answer if mode == "router" else dict(empty),
                },
            },
        }
    )


def save(store, value):
    return model_roles.save(
        store,
        model_roles.Update(
            expected_revision=model_roles.load(store)["revision"], configuration=value
        ),
    )


def private(store, sid, agent="companion"):
    session_settings.save(
        store,
        sid,
        session_settings.Update(
            expected_revision=0,
            settings=session_settings.Settings(
                agentId=agent,
                privacy=session_settings.Privacy(memoryDisabled=True, harnessDisabled=True),
            ),
        ),
    )


def test_frozen_role_configuration_and_actual_provider_evidence(tmp_path):
    with closing(Store(tmp_path / "test.db")) as store:
        sid = store.create_session()
        save(
            store, config(primary="b")
        )  # defaultModelId remains a; role assignment is authoritative.
        request = "frozen_model_request_001"
        submissions.reserve(store, request, sid, "Question", {})
        save(store, config(primary="a"))
        receipt = submissions.bind(store, request)
        local, hosted = Adapter("local"), Adapter("hosted")
        loop = Loop(
            store, Router(local_provider=local, hosted_provider=hosted, local_model="legacy")
        )
        result = loop._run_bound(sid, "Question", receipt["turn_id"], {}, None)
        assert not local.calls and hosted.calls[0][1:] == ("actual-b", 1.7)
        frozen = session_settings.execution(store, sid, receipt["turn_id"])
        assert frozen["modelConfigurationRevision"] == 1
        assert result["route"]["tier"] == "configured"
        evidence = json.loads(store.get_turn(receipt["turn_id"])["detail"])
        assert evidence["attempts"][-1]["actualModel"] == "actual-b"
        assert evidence["modelConfigurationRevision"] == 1


def test_manual_lock_failure_never_falls_back(tmp_path):
    with closing(Store(tmp_path / "test.db")) as store:
        sid = store.create_session()
        save(store, config())
        local, hosted = Adapter("local", fail=True), Adapter("hosted")
        with pytest.raises(TurnFailed, match="unapproved substitute"):
            Loop(
                store, Router(local_provider=local, hosted_provider=hosted, local_model="legacy")
            ).run_turn(sid, "Question")
        assert len(local.calls) == 1 and not hosted.calls


def test_no_harness_blocks_routing_helper_but_allows_hosted_manual_answer(tmp_path):
    with closing(Store(tmp_path / "test.db")) as store:
        sid = store.create_session()
        private(store, sid)
        save(store, config(mode="router"))
        local, hosted = Adapter("local", '{"modelId":"b"}'), Adapter("hosted")
        loop = Loop(
            store, Router(local_provider=local, hosted_provider=hosted, local_model="legacy")
        )
        with pytest.raises(TurnFailed, match="No harness"):
            loop.run_turn(sid, "Question")
        assert not local.calls and not hosted.calls
        save(store, config(primary="b"))
        assert loop.run_turn(sid, "Manual answer")["message"]["content"] == "Answer"
        assert not local.calls and hosted.calls[0][1] == "actual-b"


def test_agent_override_uses_catalogue_and_skips_helper(tmp_path):
    with closing(Store(tmp_path / "test.db")) as store:
        sid = store.create_session()
        agent = agents.create(
            store,
            agents.AgentInput(
                name="Reader",
                role="Research",
                instructions="Cite evidence.",
                modelId="b",
                toolIds=[],
                memory=agents.MemorySelection(scope="none", memoryIds=[]),
            ),
        )
        private(store, sid, agent["id"])
        save(store, config(mode="router"))
        local, hosted = Adapter("local"), Adapter("hosted")
        loop = Loop(
            store, Router(local_provider=local, hosted_provider=hosted, local_model="legacy")
        )
        assert loop.run_turn(sid, "Question")["message"]["content"] == "Answer"
        assert not local.calls and hosted.calls[0][1] == "actual-b"


def test_execution_identity_cannot_cross_sessions(tmp_path):
    with closing(Store(tmp_path / "test.db")) as store:
        first, second = store.create_session(), store.create_session()
        request = "private_origin_request_001"
        submissions.reserve(store, request, first, "Question", {})
        receipt = submissions.bind(store, request)
        for kwargs in (
            {"turn_id": receipt["turn_id"]},
            {"request_id": request},
            {"turn_id": "missing"},
        ):
            with pytest.raises(agents.AgentError, match="does not belong"):
                session_settings.execution(store, second, **kwargs)
        with pytest.raises(agents.AgentError, match="one execution"):
            session_settings.execution(store, first, turn_id=receipt["turn_id"], request_id=request)


def test_legacy_snapshot_does_not_gain_later_role_configuration(tmp_path):
    with closing(Store(tmp_path / "test.db")) as store:
        sid = store.create_session()
        request = "legacy_snapshot_request_001"
        submissions.reserve(store, request, sid, "Question", {})
        save(store, config(primary="b"))
        receipt = submissions.bind(store, request)
        local, hosted = Adapter("local"), Adapter("hosted")
        loop = Loop(
            store, Router(local_provider=local, hosted_provider=hosted, local_model="legacy")
        )
        loop._run_bound(sid, "Question", receipt["turn_id"], {}, None)
        assert local.calls[0][1:] == ("legacy", None) and not hosted.calls


def test_configured_summary_role_and_privacy(tmp_path):
    with closing(Store(tmp_path / "test.db")) as store:
        sid = store.create_session()
        value = config()
        value.roleSettings.roles["summarization"] = value.roleSettings.roles["answer"].model_copy(
            update={"modelId": "b"}
        )
        save(store, value)
        local, hosted = Adapter("local"), Adapter("hosted", "Summary")
        loop = Loop(
            store, Router(local_provider=local, hosted_provider=hosted, local_model="legacy")
        )
        child = loop.fork(sid)
        assert store.get_session(child)["summary"] == "Summary"
        assert hosted.calls[0][1:] == ("actual-b", 1.7) and not local.calls
        private(store, child)
        with pytest.raises(TurnFailed, match="No harness"):
            loop.fork(child)
        assert len(hosted.calls) == 1
