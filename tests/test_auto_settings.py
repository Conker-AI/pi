"""Auto mode proposals: typed choices from the routing role, refused without helpers, fail closed."""

import json
from contextlib import closing

import pytest

from pi.api.browser_contract import runtime_allowed, session_only_write
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.models import auto_settings
from pi.models import roles as model_roles
from tests.test_model_roles import Adapter, config

VALID = {
    "complexity": "hard",
    "think": True,
    "research": "web",
    "reasons": {
        "complexity": "Comparing laptops needs careful reasoning.",
        "think": "A background check of the trade-offs helps.",
        "research": "Prices change often.",
    },
}


def configured(store, routing_enabled=True):
    value = config()
    value["roleSettings"]["roles"]["routing"] = {
        **value["roleSettings"]["roles"]["answer"],
        "enabled": routing_enabled,
    }
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(value)
        ),
    )


def propose(store, adapter, **body):
    return auto_settings.propose(
        store,
        {"one": adapter},
        auto_settings.Request(text="Best laptop under $1000?", **body),
        source_privacy=session_settings.source_privacy,
    )


def test_proposal_returns_the_typed_settings_with_reasons(tmp_path):
    adapter = Adapter(json.dumps(VALID))
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        result = propose(store, adapter)
    assert {key: result[key] for key in ("complexity", "think", "research", "reasons")} == VALID
    assert result["modelId"] == "a" and result["configurationRevision"] == 1
    assert adapter.calls == [("actual-a", 1.0)]


@pytest.mark.parametrize(
    "reply",
    [
        "not json",
        json.dumps({**VALID, "complexity": "medium"}),
        json.dumps({**VALID, "research": "maybe"}),
        json.dumps({**VALID, "think": "yes"}),
        json.dumps({**VALID, "reasons": {"complexity": "x", "think": "y"}}),
        json.dumps({**VALID, "reasons": {**VALID["reasons"], "research": " "}}),
        json.dumps({**VALID, "reasons": {**VALID["reasons"], "think": "x" * 161}}),
    ],
)
def test_anything_malformed_fails_closed(tmp_path, reply):
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        with pytest.raises(auto_settings.ProposalError) as raised:
            propose(store, Adapter(reply))
    assert raised.value.status == 503 and raised.value.code == "unavailable"


def test_no_helpers_means_no_proposal_and_no_model_call(tmp_path):
    adapter = Adapter(json.dumps(VALID))
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        with pytest.raises(auto_settings.ProposalError) as raised:
            propose(store, adapter, harness_disabled=True)
        assert raised.value.status == 409
        session = store.create_session()
        session_settings.save(
            store,
            session,
            session_settings.Update(
                expected_revision=0,
                settings={
                    "agentId": "companion",
                    "privacy": {"memoryDisabled": False, "harnessDisabled": True},
                },
            ),
        )
        with pytest.raises(auto_settings.ProposalError) as raised:
            propose(store, adapter, session_id=session)
        assert raised.value.status == 409
    assert adapter.calls == []


def test_unavailable_configurations_fail_closed(tmp_path):
    adapter = Adapter(json.dumps(VALID))
    with closing(Store(tmp_path / "pi.db")) as store:
        with pytest.raises(auto_settings.ProposalError) as raised:
            propose(store, adapter)
        assert raised.value.status == 503 and raised.value.code == "not_configured"
        with pytest.raises(auto_settings.ProposalError) as raised:
            propose(store, adapter, session_id="ses_missing")
        assert raised.value.status == 404
    with closing(Store(tmp_path / "other.db")) as store:
        configured(store, routing_enabled=False)
        with pytest.raises(auto_settings.ProposalError) as raised:
            propose(store, adapter)
        assert raised.value.status == 503 and raised.value.code == "unavailable"
    assert adapter.calls == []


def test_the_browser_may_ask_for_a_proposal_without_a_password_proof():
    assert runtime_allowed("POST", "/models/auto-settings")
    assert session_only_write("POST", "/models/auto-settings")
    assert not runtime_allowed("GET", "/models/auto-settings")
    assert not runtime_allowed("POST", "/models/auto-settings/extra")


class Recording:
    def __init__(self, response):
        self.response, self.messages = response, []

    def complete_bounded(self, messages, *, model, timeout):
        from pi.models.providers import Completion

        self.messages.append(messages)
        return Completion(text=self.response, model=model, provider="recording")


def test_hard_complexity_reaches_the_routing_model_as_an_owner_hint():
    from pi.models.providers import Message
    from pi.models.routing import TurnContext

    value = config()
    value["roleSettings"]["answerMode"] = "router"
    value["roleSettings"]["roles"]["routing"] = {**value["roleSettings"]["roles"]["answer"]}
    router, answer = Recording('{"modelId":"b"}'), Recording("answer")
    task = [Message("user", "Compare these laptops")]
    for ctx, expected in [
        (TurnContext(is_analysis=True), {"complexity": "hard"}),
        (TurnContext(), None),
    ]:
        router.messages.clear()
        model_roles.dispatch(
            value, "answer", task, {"one": router, "two": answer}, hints=ctx.owner_hints()
        )
        sent = json.loads(router.messages[0][1].content)
        assert sent.get("ownerHints") == expected
        assert sent["task"] == [{"role": "user", "content": "Compare these laptops"}]


WITH_EFFORT = {**VALID, "effort": "high", "reasons": {**VALID["reasons"], "effort": "Needs care."}}


def test_a_proposal_may_carry_an_effort_with_its_reason(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        result = propose(store, Adapter(json.dumps(WITH_EFFORT)))
    assert result["effort"] == "high" and result["reasons"]["effort"] == "Needs care."


def test_a_proposal_without_effort_is_still_good_and_carries_none(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        result = propose(store, Adapter(json.dumps(VALID)))
    assert "effort" not in result and "effort" not in result["reasons"]


def test_a_reason_for_an_effort_that_was_not_chosen_is_dropped_not_fatal(tmp_path):
    reply = {**VALID, "reasons": {**VALID["reasons"], "effort": "Needs care."}}
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        result = propose(store, Adapter(json.dumps(reply)))
    assert "effort" not in result and "effort" not in result["reasons"]
    assert result["complexity"] == VALID["complexity"]


@pytest.mark.parametrize(
    "reply",
    [
        json.dumps({**WITH_EFFORT, "effort": "xhigh"}),
        json.dumps({**WITH_EFFORT, "effort": "auto"}),
        json.dumps({**WITH_EFFORT, "effort": None}),
        json.dumps({**WITH_EFFORT, "reasons": VALID["reasons"]}),
        json.dumps({**WITH_EFFORT, "reasons": {**WITH_EFFORT["reasons"], "effort": " "}}),
    ],
)
def test_a_malformed_effort_fails_closed(tmp_path, reply):
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        with pytest.raises(auto_settings.ProposalError) as raised:
            propose(store, Adapter(reply))
    assert raised.value.status == 503 and raised.value.code == "unavailable"


def test_the_model_is_chosen_from_the_answer_models_the_owner_offers(tmp_path):
    reply = {
        **VALID,
        "model": "b",
        "reasons": {**VALID["reasons"], "model": "A small one is enough."},
    }
    adapter = Adapter(json.dumps(reply))
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        result = propose(store, adapter)
    assert result["answerModelId"] == "b" and result["reasons"]["model"] == "A small one is enough."
    assert result["modelId"] == "a", "modelId stays the routing model that made the proposal"


def test_a_model_that_is_not_on_offer_is_dropped_not_fatal(tmp_path):
    reply = {**VALID, "model": "zzz", "reasons": {**VALID["reasons"], "model": "Because."}}
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        result = propose(store, Adapter(json.dumps(reply)))
    assert "answerModelId" not in result and "model" not in result["reasons"]
    assert result["complexity"] == VALID["complexity"]


def test_the_mode_and_the_models_reach_the_routing_model_as_data():
    models = [{"id": "a", "name": "A", "about": "A"}, {"id": "b", "name": "B", "about": "B"}]
    value = config()
    assert auto_settings.answer_models(value) == models
    value["roleSettings"]["roles"]["answer"]["eligibleModelIds"] = ["a"]
    assert auto_settings.answer_models(value) == [], "one model is no choice"
    for mode in auto_settings.MODES:
        assert auto_settings.MODE_INSTRUCTIONS[mode]


def test_the_routing_model_is_told_the_mode(tmp_path):
    seen = Recording(json.dumps(VALID))
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        auto_settings.propose(
            store,
            {"one": seen},
            auto_settings.Request(text="Best laptop under $1000?", mode="deep"),
            source_privacy=session_settings.source_privacy,
        )
    sent = json.loads(seen.messages[0][1].content)
    assert sent["guidance"] == auto_settings.MODE_INSTRUCTIONS["deep"]
    assert [m["id"] for m in sent["models"]] == ["a", "b"]
    with pytest.raises(ValueError):
        auto_settings.Request(text="x", mode="reckless")
