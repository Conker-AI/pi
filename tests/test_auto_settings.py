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
        store, {"one": adapter}, auto_settings.Request(text="Best laptop under $1000?", **body)
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
        json.dumps({**VALID, "model": "b"}),
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
