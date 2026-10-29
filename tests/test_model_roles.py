from contextlib import closing

import pytest
from pydantic import ValidationError

from pi.conversation.store import Store
from pi.models import roles as r
from pi.models.providers import Completion, Message, ProviderUnavailable


class Adapter:
    def __init__(self, response="answer", fail=False):
        self.response, self.fail, self.calls = response, fail, []

    def complete_bounded(self, messages, *, model, timeout):
        self.calls.append((model, timeout))
        if self.fail:
            raise ProviderUnavailable("offline")
        return Completion(text=self.response, model=model, provider="adapter")


def config():
    disabled = dict(
        enabled=False,
        eligibleModelIds=[],
        modelId=None,
        timeoutMs=1000,
        failure="stop",
        fallbackModelId=None,
    )
    return dict(
        providers=[
            dict(id="one", name="Provider", enabled=True),
            dict(id="two", name="Other", enabled=True),
        ],
        models=[
            dict(
                id="a",
                providerId="one",
                name="A",
                route="actual-a",
                enabled=True,
                routingDescription="",
            ),
            dict(
                id="b",
                providerId="two",
                name="B",
                route="actual-b",
                enabled=True,
                routingDescription="",
            ),
        ],
        defaultModelId="a",
        roleSettings=dict(
            answerMode="manual",
            roles={
                **{role: dict(disabled) for role in r.ROLES},
                "answer": {
                    **disabled,
                    "enabled": True,
                    "eligibleModelIds": ["a", "b"],
                    "modelId": "a",
                },
            },
        ),
    )


def test_manual_lock_exact_route_timeout_no_fallback():
    first, second = Adapter(fail=True), Adapter()
    with pytest.raises(r.SelectionError):
        r.dispatch(config(), "answer", [Message("user", "Hi")], {"one": first, "two": second})
    assert first.calls == [("actual-a", 1.0)] and second.calls == []
    result = r.dispatch(
        config(), "answer", [], {"two": second}, override="b", harness_disabled=True
    )
    assert result["completion"].model == "actual-b"
    assert result["attempts"][0]["actualModel"] == "actual-b"


def test_unknown_override_and_server_eligibility_fail_closed():
    adapter = Adapter()
    for args in [dict(override="not-known"), dict(override="a", allowed_model_ids=["b"])]:
        with pytest.raises(r.SelectionError):
            r.dispatch(config(), "answer", [], {"one": adapter}, **args)
    assert not adapter.calls


def test_replaceable_router_typed_choice_and_no_harness():
    value = config()
    value["roleSettings"]["answerMode"] = "router"
    value["roleSettings"]["roles"]["routing"] = {**value["roleSettings"]["roles"]["answer"]}
    first, second = Adapter('{"modelId":"b"}'), Adapter()
    result = r.dispatch(value, "answer", [Message("user", "Task")], {"one": first, "two": second})
    assert result["modelId"] == "b" and len(result["attempts"]) == 2
    first.response = '{"modelId":"outside"}'
    with pytest.raises(r.SelectionError):
        r.dispatch(value, "answer", [], {"one": first, "two": second})
    with pytest.raises(r.SelectionError):
        r.dispatch(value, "answer", [], {"one": first}, harness_disabled=True)


def test_explicit_helper_fallback_and_privacy():
    value = config()
    value["roleSettings"]["roles"]["summarization"] = {
        **value["roleSettings"]["roles"]["answer"],
        "failure": "fallback",
        "fallbackModelId": "b",
    }
    result = r.dispatch(value, "summarization", [], {"one": Adapter(fail=True), "two": Adapter()})
    assert result["modelId"] == "b"
    with pytest.raises(r.SelectionError):
        r.dispatch(value, "summarization", [], {"two": Adapter()}, harness_disabled=True)


def test_storage_revision_validation_and_restart(tmp_path):
    path = tmp_path / "roles.db"
    with closing(Store(path)) as store:
        with store._connect() as db:
            db.executescript(r.SCHEMA)
        r.save(
            store,
            r.Update(expected_revision=0, configuration=r.Configuration.model_validate(config())),
        )
        with pytest.raises(ValueError):
            r.save(
                store,
                r.Update(
                    expected_revision=0, configuration=r.Configuration.model_validate(config())
                ),
            )
    with closing(Store(path)) as store:
        assert r.load(store)["configuration"] == config()
    value = config()
    value["roleSettings"]["roles"]["answer"]["fallbackModelId"] = "b"
    with pytest.raises(ValidationError):
        r.Configuration.model_validate(value)


@pytest.mark.parametrize("provider_fails", [False, True])
def test_interruption_during_attempt_does_not_fall_back(provider_fails):
    value = config()
    value["roleSettings"]["roles"]["summarization"] = {
        **value["roleSettings"]["roles"]["answer"],
        "failure": "fallback",
        "fallbackModelId": "b",
    }
    first, second = Adapter(fail=provider_fails), Adapter()

    def guard():
        if first.calls:
            raise ProviderUnavailable("Call interrupted")

    with pytest.raises(ProviderUnavailable, match="Call interrupted"):
        r.dispatch(value, "summarization", [], {"one": first, "two": second}, guard=guard)
    assert len(first.calls) == 1
    assert second.calls == []


def test_interruption_during_router_prevents_answer():
    value = config()
    value["roleSettings"]["answerMode"] = "router"
    value["roleSettings"]["roles"]["routing"] = {**value["roleSettings"]["roles"]["answer"]}
    first, second = Adapter('{"modelId":"b"}'), Adapter()

    def guard():
        if first.calls:
            raise ProviderUnavailable("Call interrupted")

    with pytest.raises(ProviderUnavailable, match="Call interrupted"):
        r.dispatch(value, "answer", [], {"one": first, "two": second}, guard=guard)
    assert len(first.calls) == 1
    assert not second.calls


def test_interruption_before_provider_prevents_call():
    adapter = Adapter()

    def guard():
        raise ProviderUnavailable("Call paused")

    with pytest.raises(ProviderUnavailable, match="Call paused"):
        r.dispatch(config(), "answer", [], {"one": adapter}, guard=guard)
    assert not adapter.calls


def test_single_answer_candidate_skips_classifier_but_preserves_privacy():
    value = config()
    value["roleSettings"]["answerMode"] = "router"
    value["roleSettings"]["roles"]["routing"] = {**value["roleSettings"]["roles"]["answer"]}
    value["roleSettings"]["roles"]["answer"]["eligibleModelIds"] = ["a"]
    adapter = Adapter()
    result = r.dispatch(value, "answer", [Message("user", "Hello")], {"one": adapter})
    assert len(adapter.calls) == 1
    assert result["modelId"] == "a"
    assert result["attempts"][0]["selectionReason"] == "single_eligible_model"
    with pytest.raises(r.SelectionError, match="No harness"):
        r.dispatch(value, "answer", [], {"one": adapter}, harness_disabled=True)
    assert len(adapter.calls) == 1


def test_decision_classifier_cannot_be_saved_as_talking_model_but_old_data_is_readable():
    value = config()
    value["providers"][0]["id"] = "decisions"
    value["models"][0].update(providerId="decisions", route="model-routing")
    r.Configuration.model_validate(value)
    with pytest.raises(ValidationError, match="default talking model"):
        r.Update(expected_revision=0, configuration=value)


def test_laya_shortlist_rejects_nine_candidates_at_save():
    value = config()
    value["providers"].append(dict(id="decisions", name="Laya", enabled=True))
    value["models"].append(
        dict(id="router", providerId="decisions", name="Laya", route="model-routing", enabled=True)
    )
    value["models"].extend(
        dict(id=f"m{i}", providerId="one", name=f"M{i}", route=f"model{i}", enabled=True)
        for i in range(7)
    )
    roles = value["roleSettings"]["roles"]
    roles["answer"]["eligibleModelIds"] += [f"m{i}" for i in range(7)]
    roles["routing"] = {**roles["answer"], "eligibleModelIds": ["router"], "modelId": "router"}
    value["roleSettings"]["answerMode"] = "router"
    with pytest.raises(ValidationError, match="at most eight"):
        r.Update(expected_revision=0, configuration=value)
    roles["answer"]["eligibleModelIds"].pop()
    r.Update(expected_revision=0, configuration=value)


def test_decision_route_must_match_assigned_helper_role():
    value = config()
    value["providers"].append(dict(id="decisions", name="Laya", enabled=True))
    value["models"].append(
        dict(
            id="ranker",
            providerId="decisions",
            name="Ranking",
            route="memory-ranking",
            enabled=True,
        )
    )
    value["roleSettings"]["roles"]["summarization"].update(eligibleModelIds=["ranker"])
    with pytest.raises(ValidationError, match="summarization role"):
        r.Update(expected_revision=0, configuration=value)


def test_service_roles_are_added_to_older_settings_with_the_local_wait():
    value = config()
    for role in r.LOCAL_ROLES:
        del value["roleSettings"]["roles"][role]
    roles = r.Configuration.model_validate(value).roleSettings.roles
    for role in ("memory-sorting", "tool-text"):
        assert role in r.ROLES and role in r.LATER_ROLES
        assert not roles[role].enabled and roles[role].modelId is None
        assert roles[role].timeoutMs == r.LOCAL_TIMEOUT_MS == 300000
    # Roles already saved keep their own wait.
    assert roles["summarization"].timeoutMs == 1000


def test_role_waits_go_up_to_ten_minutes():
    value = config()
    value["roleSettings"]["roles"]["answer"]["timeoutMs"] = 600000
    r.Configuration.model_validate(value)
    adapter = Adapter()
    r.dispatch(value, "answer", [], {"one": adapter})
    assert adapter.calls == [("actual-a", 600.0)]
    value["roleSettings"]["roles"]["answer"]["timeoutMs"] = 600001
    with pytest.raises(ValidationError):
        r.Configuration.model_validate(value)


def test_default_waits_when_nothing_was_saved():
    assert r.default_timeout_ms("memory-ranking") == 2000
    assert r.default_timeout_ms("answer") == 120000
    assert r.default_timeout_ms("answer", local=True) == 300000
    assert r.default_timeout_ms("tool-text") == r.default_timeout_ms("memory-sorting") == 300000
