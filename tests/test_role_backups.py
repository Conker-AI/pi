"""Backup models: a role tries its own model, then each backup in the owner's order."""

import json
from contextlib import closing

import pytest
from pydantic import ValidationError

from pi.agents import workflow_drafts
from pi.conversation.store import Store
from pi.models import roles as model_roles
from pi.models.providers import Completion, Message, ProviderUnavailable

MESSAGES = [Message("user", "Summarise this.")]


class Adapter:
    """Answers with the model's route, or a set reply; routes in `down` are unavailable."""

    name = "one"

    def __init__(self, *down, replies=None):
        self.down, self.replies, self.calls = set(down), replies or {}, []

    def complete_bounded(self, messages, *, model, timeout):
        self.calls.append(model)
        if model in self.down:
            raise ProviderUnavailable("Unavailable")
        return Completion(text=self.replies.get(model, model), model=model, provider=self.name)


def off():
    return dict(
        enabled=False,
        eligibleModelIds=[],
        modelId=None,
        timeoutMs=1000,
        failure="stop",
        fallbackModelIds=[],
    )


def config(**summary):
    """Summarization on model a with backups b then c; model `off` is switched off."""
    role = {
        **off(),
        "enabled": True,
        "eligibleModelIds": ["a", "b", "c", "d", "off"],
        "modelId": "a",
        "failure": "fallback",
        "fallbackModelIds": ["b", "c"],
        **summary,
    }
    return {
        "providers": [{"id": "one", "name": "One", "enabled": True}],
        "models": [
            {
                "id": key,
                "providerId": "one",
                "name": key.upper(),
                "route": f"route-{key}",
                "enabled": key != "off",
            }
            for key in ("a", "b", "c", "d", "off")
        ],
        "defaultModelId": "a",
        "roleSettings": {
            "answerMode": "manual",
            "roles": {**{name: off() for name in model_roles.ROLES}, "summarization": role},
        },
    }


def router_config():
    """Answer picked by Jev (model d) from a, b and c, with backups c then b."""
    value = config()
    roles = value["roleSettings"]["roles"]
    roles["answer"] = {
        **off(),
        "enabled": True,
        "eligibleModelIds": ["a", "b", "c"],
        "modelId": "a",
        "failure": "fallback",
        "fallbackModelIds": ["c", "b"],
    }
    roles["routing"] = {**off(), "enabled": True, "eligibleModelIds": ["d"], "modelId": "d"}
    value["roleSettings"]["answerMode"] = "router"
    return value


def test_backups_are_tried_in_order():
    adapter = Adapter("route-a", "route-b")
    result = model_roles.dispatch(config(), "summarization", MESSAGES, {"one": adapter})
    assert adapter.calls == ["route-a", "route-b", "route-c"]
    assert result["modelId"] == "c" and result["completion"].text == "route-c"
    assert [(item["modelId"], item["status"]) for item in result["attempts"]] == [
        ("a", "unavailable"),
        ("b", "unavailable"),
        ("c", "completed"),
    ]


def test_first_working_backup_answers_and_later_ones_are_not_called():
    adapter = Adapter("route-a")
    result = model_roles.dispatch(config(), "summarization", MESSAGES, {"one": adapter})
    assert adapter.calls == ["route-a", "route-b"] and result["modelId"] == "b"


def test_every_model_down_refuses_without_a_substitute():
    adapter = Adapter("route-a", "route-b", "route-c")
    with pytest.raises(model_roles.SelectionError, match="unapproved substitute"):
        model_roles.dispatch(config(), "summarization", MESSAGES, {"one": adapter})
    assert adapter.calls == ["route-a", "route-b", "route-c"]


def test_stop_never_tries_a_backup():
    adapter = Adapter("route-a")
    stop = config(failure="stop", fallbackModelIds=[])
    with pytest.raises(model_roles.SelectionError):
        model_roles.dispatch(stop, "summarization", MESSAGES, {"one": adapter})
    assert adapter.calls == ["route-a"]


def test_router_failure_starts_with_the_first_backup():
    adapter = Adapter("route-d", "route-c")
    result = model_roles.dispatch(router_config(), "answer", MESSAGES, {"one": adapter})
    assert adapter.calls == ["route-d", "route-c", "route-b"] and result["modelId"] == "b"
    assert result["attempts"][0] == {
        "role": "routing",
        "modelId": "d",
        "status": "unavailable",
        "fallbackModelId": "c",
    }


def test_router_choice_is_followed_by_the_backups():
    adapter = Adapter("route-b", replies={"route-d": json.dumps({"modelId": "b"})})
    result = model_roles.dispatch(router_config(), "answer", MESSAGES, {"one": adapter})
    assert adapter.calls == ["route-d", "route-b", "route-c"] and result["modelId"] == "c"


def test_single_fallback_reads_as_a_list():
    legacy = {**off(), "fallbackModelId": None}
    del legacy["fallbackModelIds"]
    assert model_roles.Assignment.model_validate(legacy).fallbackModelIds == []
    one = {**legacy, "enabled": True, "eligibleModelIds": ["a", "b"], "modelId": "a"}
    one.update(failure="fallback", fallbackModelId="b")
    assignment = model_roles.Assignment.model_validate(one)
    assert assignment.fallbackModelIds == ["b"]
    assert "fallbackModelId" not in assignment.model_dump()
    # An older client spreads what it read and sets the one fallback it knows about.
    both = {**one, "fallbackModelIds": ["b", "c"], "fallbackModelId": None, "failure": "stop"}
    assert model_roles.Assignment.model_validate(both).fallbackModelIds == []


def test_saved_single_fallback_loads_as_a_list(tmp_path):
    legacy = config()
    for name, role in legacy["roleSettings"]["roles"].items():
        backups = role.pop("fallbackModelIds")
        role["fallbackModelId"] = backups[0] if name == "summarization" else None
    with closing(Store(tmp_path / "test.db")) as store:
        with store._connect() as db:
            db.execute(
                "INSERT INTO model_role_settings VALUES(1,1,?)",
                (json.dumps(legacy),),
            )
        roles = model_roles.load(store)["configuration"]["roleSettings"]["roles"]
    assert roles["summarization"]["fallbackModelIds"] == ["b"]
    assert all(
        role["fallbackModelIds"] == [] for name, role in roles.items() if name != "summarization"
    )
    assert not any("fallbackModelId" in role for role in roles.values())


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"fallbackModelIds": ["b", "b"]}, "listed once"),
        ({"fallbackModelIds": ["b", "a"]}, "differ from primary"),
        ({"failure": "stop"}, "Stop-on-failure"),
        ({"fallbackModelIds": []}, "enabled primary/fallback"),
        ({"fallbackModelIds": ["b", "off"]}, "enabled primary/fallback"),
        ({"eligibleModelIds": ["a", "b"]}, "explicitly eligible"),
        ({"fallbackModelIds": ["b", "c", "d", "b", "c", "d", "b", "c", "d"]}, "at most 8"),
    ],
)
def test_backup_rules(change, message):
    with pytest.raises(ValidationError, match=message):
        model_roles.Configuration.model_validate(config(**change))


def test_waits_count_every_model_a_role_may_try():
    roles = config()["roleSettings"]["roles"]
    assert model_roles.tries(roles["summarization"]) == 3
    assert model_roles.tries(roles["answer"]) == 1
    assert workflow_drafts.attempt_seconds(config(), "summarization") == 3.0
