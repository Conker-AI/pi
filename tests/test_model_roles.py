from contextlib import closing

import pytest
from pydantic import ValidationError

from pi import model_roles as r
from pi.providers import Completion, Message, ProviderUnavailable
from pi.store import Store


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
            dict(id="a", providerId="one", name="A", route="actual-a", enabled=True),
            dict(id="b", providerId="two", name="B", route="actual-b", enabled=True),
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
