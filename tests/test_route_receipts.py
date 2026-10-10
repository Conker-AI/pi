"""Selection evidence, fallback, and stronger tiers are distinct public facts."""

import json
from contextlib import closing

import pytest
from test_model_role_execution import Adapter, config, save
from test_tool_turns import CALL, FakeGate

from pi import model_roles, response_retries
from pi.loop import ActedWithoutReply, Loop
from pi.providers import ProviderUnavailable
from pi.routing import Reason, Router, Tier, TurnContext
from pi.store import Store


def automatic(*, answer_fallback=False):
    value = config(mode="router").model_dump()
    value["providers"].append(dict(id="selector", name="Selector", enabled=True))
    value["models"].append(
        dict(id="choice", providerId="selector", name="Choice", route="choice", enabled=True)
    )
    value["roleSettings"]["roles"]["routing"].update(
        eligibleModelIds=["choice"], modelId="choice"
    )
    if answer_fallback:
        value["roleSettings"]["roles"]["answer"].update(
            failure="fallback", fallbackModelId="b"
        )
    return model_roles.Configuration.model_validate(value)


@pytest.mark.parametrize("primary", ["a", "b"])
def test_manual_model_selection_is_not_escalation_or_a_skipped_attempt(tmp_path, primary):
    with closing(Store(tmp_path / "pi.db")) as store:
        save(store, config(primary=primary))
        local, hosted = Adapter("local"), Adapter("hosted")
        loop = Loop(store, Router(providers={"local": local, "hosted": hosted}))
        result = loop.run_turn(store.create_session(), "One answer")
        receipt = result["route"]
        assert receipt["tier"] == "configured" and receipt["reason"] == "configured"
        assert receipt["escalated"] is False and receipt["fallback_used"] is False
        assert receipt["skipped"] == []
        assert receipt["selection"] == json.loads(store.get_turn(result["turn_id"])["detail"])
        assert receipt["selection"]["modelId"] == primary
        assert [a["status"] for a in receipt["selection"]["attempts"]] == ["completed"]
        assert len(local.calls) + len(hosted.calls) == 1


@pytest.mark.parametrize("failed", [None, "answer", "routing"])
def test_automatic_selection_reports_only_real_unavailable_attempts(tmp_path, failed):
    with closing(Store(tmp_path / "pi.db")) as store:
        save(store, automatic(answer_fallback=failed is not None))
        selector = Adapter("selector", '{"modelId":"a"}', fail=failed == "routing")
        local, hosted = Adapter("local", fail=failed == "answer"), Adapter("hosted")
        loop = Loop(store, Router(providers={
            "local": local, "hosted": hosted, "selector": selector,
        }))
        result = loop.run_turn(store.create_session(), "Choose a model")
        receipt = result["route"]
        assert receipt["escalated"] is False
        assert receipt["fallback_used"] is (failed is not None)
        expected = [] if failed is None else [
            "answer: a unavailable" if failed == "answer" else "routing: choice unavailable"
        ]
        assert receipt["skipped"] == expected
        evidence = receipt["selection"]
        assert evidence == json.loads(store.get_turn(result["turn_id"])["detail"])
        assert evidence["modelId"] == ("a" if failed is None else "b")
        assert all("{" not in reason for reason in receipt["skipped"])
        assert len(selector.calls) == 1
        assert len(local.calls) == (0 if failed == "routing" else 1)
        assert len(hosted.calls) == (0 if failed is None else 1)


def test_single_eligible_automatic_answer_keeps_selection_evidence(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        value = automatic()
        value.roleSettings.roles["answer"].eligibleModelIds = ["a"]
        save(store, value)
        local, selector = Adapter("local"), Adapter("selector")
        result = Loop(store, Router(providers={"local": local, "selector": selector})).run_turn(
            store.create_session(), "One option"
        )
        route = result["route"]
        assert not route["escalated"] and not route["fallback_used"] and route["skipped"] == []
        assert route["selection"]["attempts"][0]["selectionReason"] == "single_eligible_model"
        assert len(local.calls) == 1 and not selector.calls


def test_unavailable_stronger_tier_is_not_reported_as_actual_escalation():
    router = Router(local_provider=Adapter("local"), local_model="local-model")
    route = router.route(TurnContext(is_analysis=True))
    assert route.tier is Tier.LOCAL and route.reason is Reason.ANALYSIS
    assert route.escalated is False


def test_selected_retry_keeps_the_historical_json_receipt(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        save(store, config())
        local, hosted = Adapter("local"), Adapter("hosted")
        loop = Loop(store, Router(providers={"local": local, "hosted": hosted}))
        sid = store.create_session()
        first = loop.run_turn(sid, "Original")
        result = response_retries.run(loop, sid, first["message"]["id"], response_retries.Retry(
            request_id="route_retry_0001", model_id="b",
        ))
        evidence = json.loads(store.get_turn(result["turn_id"])["detail"])
        assert evidence["modelId"] == "b" and evidence["modelConfigurationRevision"] == 1
        assert evidence["attempts"][-1]["actualModel"] == "actual-b"
        assert len(local.calls) == len(hosted.calls) == 1


def test_action_reply_recovery_keeps_evidence_without_repeating_effects(tmp_path):
    class Scripted(Adapter):
        def __init__(self):
            super().__init__("local")
            self.replies = iter([CALL, ProviderUnavailable("offline"), "Saved action completed."])

        def complete_bounded(self, messages, *, model, timeout):
            self.response = next(self.replies)
            if isinstance(self.response, Exception):
                self.calls.append((messages, model, timeout))
                raise self.response
            return super().complete_bounded(messages, model=model, timeout=timeout)

    with closing(Store(tmp_path / "pi.db")) as store:
        save(store, config())
        provider, gate = Scripted(), FakeGate()
        loop = Loop(store, Router(providers={"local": provider}), toolgate=gate)
        with pytest.raises(ActedWithoutReply) as error:
            loop.run_turn(store.create_session(), "Act once")
        result = loop.recover_reply(error.value.turn_id, "route_reply_0001")
        assert result["status"] == "complete" and len(gate.invocations) == 1
        evidence = json.loads(store.get_turn(result["turn_id"])["detail"])
        assert evidence["attempts"][-1]["actualModel"] == "actual-a"
        assert len(provider.calls) == 3
