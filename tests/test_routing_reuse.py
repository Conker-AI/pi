"""Identical typed routing can reuse a decision, never another inference receipt."""

import json
import threading
from contextlib import closing
from contextvars import copy_context
from copy import deepcopy

import httpx
import pytest
from test_decision_provider import setup
from test_model_roles import Adapter
from test_tool_turns import CALL, FakeGate

from pi import (
    background_thinking,
    inference_control,
    model_roles,
    research_usage,
    routing_memo,
    turn_control,
    turn_steering,
)
from pi.decision_provider import DecisionProvider
from pi.loop import ActedWithoutReply, Loop
from pi.providers import Completion, Message, ProviderUnavailable
from pi.routing import Router
from pi.store import Store


class Answers:
    name = "one"

    def __init__(self, replies):
        self.replies, self.calls = iter(replies), []
        self.before = None

    def complete_bounded(self, messages, *, model, timeout):
        self.calls.append(messages)
        if self.before:
            self.before()
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return Completion(reply, model, self.name, input_tokens=4, output_tokens=3)


def response():
    return httpx.Response(
        200,
        json={
            "choice": "a",
            "confidence": 0.9,
            "model": "synthetic-laya",
            "elapsed_ms": 321,
            "probabilities": {"a": 0.9, "b": 0.1},
        },
    )


def configured(handler=None):
    requests = []

    def collect(request):
        requests.append(json.loads(request.content))
        return handler(request) if handler else response()

    value, decision = setup(httpx.MockTransport(collect))
    value["roleSettings"]["roles"]["routing"].update(
        eligibleModelIds=["laya"], failure="stop", fallbackModelId=None
    )
    return value, decision, requests


def rig(store, replies, gate=None):
    value, decision, requests = configured()
    model_roles.save(store, model_roles.Update(expected_revision=0, configuration=value))
    answer = Answers(replies)
    loop = Loop(
        store,
        Router(providers={"decisions": decision, "one": answer, "two": Adapter()}),
        toolgate=gate or FakeGate(),
    )
    return loop, answer, requests


def dispatch(value, decision, memo, messages=None, *, revision=1, **kwargs):
    return model_roles.dispatch(
        value,
        "answer",
        messages or [Message("user", "Synthetic request")],
        {"decisions": decision, "one": Adapter(), "two": Adapter()},
        routing_memo=memo,
        configuration_revision=revision,
        guard=inference_control.check,
        **kwargs,
    )


@pytest.mark.parametrize("steps", [1, 2])
def test_full_tool_turn_reuses_identical_routing_without_duplicate_usage(tmp_path, steps):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, answer, requests = rig(store, [CALL] * steps + ["Done."])
        result = loop.run_turn(store.create_session(), "Use the echo tool and report.")
        assert len(loop.toolgate.invocations) == steps
        assert len(answer.calls) == steps + 1
        assert len(requests) == 1
        receipt = research_usage.receipt(store, result["turn_id"])
        assert [row["role"] for row in receipt["attempts"]] == ["routing"] + ["answer"] * (
            steps + 1
        )
        assert receipt["foreground"]["attempt_count"] == steps + 2
        assert receipt["foreground"]["known_subtotal"]["input_tokens"] == 4 * (steps + 1)
        assert receipt["foreground"]["unknown_counts"]["input_tokens"] == 1
        evidence = result["route"]["selection"]
        assert evidence == json.loads(store.get_turn(result["turn_id"])["detail"])
        assert evidence["attempts"][0] == {
            "role": "routing",
            "modelId": "laya",
            "providerId": "decisions",
            "requestedModel": "model-routing",
            "actualModel": "synthetic-laya",
            "status": "reused",
            "selectionReason": "same_routing_input",
            "decision": {
                "confidence": 0.9,
                "inputScope": "latest-user-request",
                "inputCharacters": 29,
            },
        }
        assert result["route"]["skipped"] == [] and not result["route"]["fallback_used"]
        assert routing_memo.current(store.path, result["turn_id"]) is None


@pytest.mark.parametrize(
    "change",
    [
        "steer",
        "description",
        "order",
        "revision",
        "timeout",
        "adapter",
        "confidence",
        "instructions",
        "long-metadata",
    ],
)
def test_changed_routing_identity_requires_another_actual_call(change, monkeypatch):
    value, decision, requests = configured()
    messages = [Message("user", "x" * 801 if change == "long-metadata" else "Synthetic request")]
    with routing_memo.scope("store", "turn") as memo:
        first = dispatch(value, decision, memo, messages)
        assert first["attempts"][0]["status"] == "completed"
        revision = 1
        if change == "steer":
            messages.append(Message("user", "Use the revised question"))
        elif change == "description":
            value["models"][0]["routingDescription"] = "New capability description"
        elif change == "order":
            value["models"] = list(reversed(value["models"]))
        elif change == "revision":
            revision = 2
        elif change == "timeout":
            value["roleSettings"]["roles"]["routing"]["timeoutMs"] = 900
        elif change == "adapter":
            decision = DecisionProvider(decision.url, decision.key, transport=decision.transport)
        elif change == "confidence":
            decision.minimum_confidence = 0.8
        elif change == "instructions":
            from pi import decision_provider

            original = decision_provider.routing_request

            def edited(*args):
                body, evidence = original(*args)
                return {**body, "instructions": "Changed routing instruction"}, evidence

            monkeypatch.setattr(decision_provider, "routing_request", edited)
        elif change == "long-metadata":
            messages.append(Message("assistant", CALL))
            messages.append(Message("tool", "synthetic tool result"))
        second = dispatch(value, decision, memo, messages, revision=revision)
        assert second["attempts"][0]["status"] == "completed"
        assert len(requests) == 2


def test_helper_failure_and_successful_fallback_are_never_reused():
    value, decision, requests = configured(lambda _: httpx.Response(503))
    value["roleSettings"]["roles"]["routing"].update(
        eligibleModelIds=["laya", "a"], failure="fallback", fallbackModelId="a"
    )
    helper, answer = Adapter('{"modelId":"b"}'), Adapter()
    with routing_memo.scope("store", "turn") as memo:
        for _ in range(2):
            result = model_roles.dispatch(
                value,
                "answer",
                [Message("user", "Same")],
                {"decisions": decision, "one": helper, "two": answer},
                routing_memo=memo,
                configuration_revision=1,
            )
            assert result["modelId"] == "b"
            assert [item["status"] for item in result["attempts"]] == [
                "unavailable",
                "completed",
                "completed",
            ]
            assert memo.value is None
    assert len(requests) == len(helper.calls) == len(answer.calls) == 2


@pytest.mark.parametrize("kind", ["invalid-choice", "unavailable"])
def test_unsuccessful_decision_does_not_poison_later_identical_retry(kind):
    count = 0

    def handler(_):
        nonlocal count
        count += 1
        if count == 1:
            if kind == "unavailable":
                return httpx.Response(503)
            value = response().json()
            value["choice"] = "outside"
            return httpx.Response(200, json=value)
        return response()

    value, decision, requests = configured(handler)
    with routing_memo.scope("store", "turn") as memo:
        with pytest.raises(model_roles.SelectionError):
            dispatch(value, decision, memo)
        assert memo.value is None
        assert dispatch(value, decision, memo)["attempts"][0]["status"] == "completed"
        assert dispatch(value, decision, memo)["attempts"][0]["status"] == "reused"
    assert len(requests) == 2


def test_cache_hit_rechecks_current_role_eligibility():
    value, decision, requests = configured()
    with routing_memo.scope("store", "turn") as memo:
        dispatch(value, decision, memo)
        with pytest.raises(model_roles.SelectionError):
            dispatch(value, decision, memo, allowed_model_ids=["a", "b"])
    assert len(requests) == 1


@pytest.mark.parametrize("during_lookup", [False, True])
def test_stop_before_or_during_hit_never_dispatches_answer(during_lookup, monkeypatch):
    value, decision, requests = configured()
    token = inference_control.Token()
    with routing_memo.scope("store", "turn") as memo, inference_control.bind(token):
        dispatch(value, decision, memo)
        if during_lookup:
            original = memo.get

            def canceled(key):
                result = original(key)
                token.cancel()
                return result

            monkeypatch.setattr(memo, "get", canceled)
        else:
            token.cancel()
        answer = Adapter()
        with pytest.raises(inference_control.InferenceCancelled):
            model_roles.dispatch(
                value,
                "answer",
                [Message("user", "Synthetic request")],
                {"decisions": decision, "one": answer},
                routing_memo=memo,
                configuration_revision=1,
                guard=inference_control.check,
            )
        assert answer.calls == []
    assert len(requests) == 1 and memo.key is memo.value is None


def test_memo_is_single_entry_private_to_attempt_store_turn_and_thread():
    value, decision, requests = configured()
    with routing_memo.scope("store", "turn") as memo:
        dispatch(value, decision, memo)
        assert routing_memo.current("other-store", "turn") is None
        assert routing_memo.current("store", "other-turn") is None
        thread_values = []
        copied = copy_context()
        worker = threading.Thread(
            target=lambda: copied.run(
                lambda: thread_values.append(routing_memo.current("store", "turn"))
            )
        )
        worker.start()
        worker.join(2)
        assert thread_values == [None]
        assert "Synthetic request" not in repr(memo.value)
        # Only the most recent successful signature is retained.
        dispatch(value, decision, memo, [Message("user", "Changed")])
        dispatch(value, decision, memo)
    assert memo.key is memo.value is None and not memo.active
    with routing_memo.scope("store", "turn") as fresh:
        dispatch(value, decision, fresh)
    assert len(requests) == 4


def test_finished_turns_and_submission_replays_do_not_share_memo(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, _, requests = rig(store, [CALL, "Done.", CALL, "Done."])
        sid = store.create_session()
        first = loop.run_turn(sid, "Same", request_id="memo_turn_request_01")
        assert loop.run_turn(sid, "Same", request_id="memo_turn_request_01")["replayed"]
        second = loop.run_turn(store.create_session(), "Same")
        assert first["turn_id"] != second["turn_id"]
        assert len(requests) == 2 and requests[0] == requests[1]


def test_steering_restarts_attempt_and_keeps_original_actual_call_usage(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, answer, requests = rig(store, ["Discarded", CALL, "Done."])
        sid = store.create_session()

        def steer():
            if len(answer.calls) == 1:
                turn = store.turns(sid)[0]["id"]
                turn_steering.submit(
                    store,
                    turn,
                    turn_steering.Steer(
                        request_id="memo_steer_request", text="Use the new instructions"
                    ),
                )

        answer.before = steer
        result = loop.run_turn(sid, "Original")
        assert len(requests) == 2 and requests[0] != requests[1]
        assert result["message"]["content"] == "Done."
        assert research_usage.receipt(store, result["turn_id"])["foreground"]["attempt_count"] == 5


def test_approval_resume_does_not_reuse_parked_decision(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, _, requests = rig(store, [CALL, "Done."], FakeGate(needs_approval=True))
        pending = loop.run_turn(store.create_session(), "Same")
        assert pending["status"] == "awaiting_approval"
        assert routing_memo.current(store.path, pending["turn_id"]) is None
        result = loop.resume_turn(pending["turn_id"])
        assert result["message"]["content"] == "Done."
        assert len(requests) == 2 and requests[0] == requests[1]


def test_action_reply_recovery_does_not_reuse_failed_attempt(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, _, requests = rig(store, [CALL, ProviderUnavailable("synthetic"), "Done."])
        with pytest.raises(ActedWithoutReply) as failure:
            loop.run_turn(store.create_session(), "Same")
        assert len(requests) == 1
        result = loop.recover_reply(failure.value.turn_id, "memo_recovery_request")
        assert result["message"]["content"] == "Done." and len(requests) == 2
        assert requests[0] == requests[1]


def test_frozen_configuration_survives_owner_settings_change_during_tool(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:

        class Gate(FakeGate):
            def invoke(self, *args, **kwargs):
                saved = model_roles.load(store)
                changed = deepcopy(saved["configuration"])
                changed["models"][0]["routingDescription"] = "Changed after submission"
                model_roles.save(
                    store, model_roles.Update(expected_revision=1, configuration=changed)
                )
                return super().invoke(*args, **kwargs)

        loop, _, requests = rig(store, [CALL, "Done."], Gate())
        result = loop.run_turn(store.create_session(), "Same")
        assert len(requests) == 1
        assert result["route"]["selection"]["modelConfigurationRevision"] == 1
        assert model_roles.load(store)["revision"] == 2


def test_durable_stop_after_tool_clears_memo_and_never_answers(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:

        class Gate(FakeGate):
            def invoke(self, *args, **kwargs):
                result = super().invoke(*args, **kwargs)
                turn_control.cancel(store, store.turns(sid)[0]["id"])
                return result

        loop, answer, requests = rig(store, [CALL, "Must not answer"], Gate())
        sid = store.create_session()
        with pytest.raises(ActedWithoutReply):
            loop.run_turn(sid, "Same")
        assert len(answer.calls) == len(requests) == 1
        assert routing_memo.current(store.path, store.turns(sid)[0]["id"]) is None


def test_wrapped_decision_provider_is_not_unwrapped_to_bypass_its_controls():
    value, decision, requests = configured()

    class Boundary:
        def __init__(self):
            self.calls = 0

        def complete_bounded(self, *args, **kwargs):
            self.calls += 1
            return decision.complete_bounded(*args, **kwargs)

    boundary = Boundary()
    with routing_memo.scope("store", "turn") as memo:
        dispatch(value, boundary, memo)
        dispatch(value, boundary, memo)
    assert len(requests) == boundary.calls == 2


def test_privacy_and_manual_override_still_win_over_a_cached_decision():
    value, decision, requests = configured()
    with routing_memo.scope("store", "turn") as memo:
        dispatch(value, decision, memo)
        with pytest.raises(model_roles.SelectionError, match="No harness"):
            dispatch(value, decision, memo, harness_disabled=True)
        overridden = dispatch(value, decision, memo, override="b", harness_disabled=True)
        assert overridden["modelId"] == "b"
        assert [item["role"] for item in overridden["attempts"]] == ["answer"]
    assert len(requests) == 1


def test_explicit_thinking_has_no_foreground_memo_or_usage_reuse(tmp_path):
    started, release = threading.Event(), threading.Event()
    with closing(Store(tmp_path / "pi.db")) as store:
        loop, _, requests = rig(store, [CALL, "Done."])
        saved = model_roles.load(store)
        saved["configuration"]["roleSettings"]["roles"]["thinking"].update(
            enabled=True, eligibleModelIds=["b"], modelId="b", timeoutMs=30000
        )
        model_roles.save(
            store, model_roles.Update(expected_revision=1, configuration=saved["configuration"])
        )
        sid = store.create_session()
        observed_memos = []

        class Thinker:
            name = "two"

            def complete_bounded(self, messages, *, model, timeout):
                turn_id = store.turns(sid)[0]["id"]
                observed_memos.append(routing_memo.current(store.path, turn_id))
                started.set()
                assert release.wait(30)
                return Completion("Findings", model, self.name)

        loop.router.providers["two"] = Thinker()
        try:
            result = loop.run_turn(sid, "Same", think=True)
            assert started.wait(2)
            assert observed_memos == [None]
            assert len(requests) == 1
        finally:
            release.set()
            with loop.thinking.lock:
                threads = list(loop.thinking.threads)
            for thread in threads:
                thread.join(5)
                assert not thread.is_alive()
        assert background_thinking.list_for_session(store, sid)[0]["status"] == "completed"
        usage = research_usage.receipt(store, result["turn_id"])
        assert usage["foreground"]["attempt_count"] == 3
        assert usage["thinking"]["attempt_count"] == 1
