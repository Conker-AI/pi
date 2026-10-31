"""Auto roles: a helper role that follows the answer role's model and backups."""

import json
from contextlib import closing

import pytest
from pydantic import ValidationError

from pi.conversation.store import Store
from pi.models import roles as model_roles
from tests.test_role_backups import MESSAGES, Adapter, config, off, router_config


def auto(value, role="summarization", **change):
    value["roleSettings"]["roles"][role] = {
        **off(),
        "enabled": True,
        "followAnswer": True,
        "timeoutMs": 4321,
        **change,
    }
    return value


def manual(answer="a"):
    value = config()
    value["roleSettings"]["roles"]["answer"] = {
        **off(),
        "enabled": True,
        "eligibleModelIds": [answer],
        "modelId": answer,
    }
    return value


def test_auto_role_is_saved_with_the_answer_model():
    role = model_roles.Configuration.model_validate(auto(manual("b"))).roleSettings.roles[
        "summarization"
    ]
    assert (role.modelId, role.eligibleModelIds, role.failure, role.fallbackModelIds) == (
        "b",
        ["b"],
        "stop",
        [],
    )
    assert role.followAnswer and role.timeoutMs == 4321


def test_auto_role_ignores_its_own_stale_model():
    stale = auto(manual("b"), eligibleModelIds=["a"], modelId="a")
    role = model_roles.Configuration.model_validate(stale).roleSettings.roles["summarization"]
    assert role.modelId == "b" and role.eligibleModelIds == ["b"]


def test_auto_role_takes_the_answer_backups_in_router_mode():
    role = model_roles.Configuration.model_validate(auto(router_config())).roleSettings.roles[
        "summarization"
    ]
    assert role.modelId == "a" and role.eligibleModelIds == ["a", "c", "b"]
    assert role.failure == "fallback" and role.fallbackModelIds == ["c", "b"]


def test_auto_role_calls_the_answer_model():
    adapter = Adapter()
    result = model_roles.dispatch(auto(manual("c")), "summarization", MESSAGES, {"one": adapter})
    assert adapter.calls == ["route-c"] and result["modelId"] == "c"


def test_a_new_answer_model_reaches_the_auto_role(tmp_path):
    with closing(Store(tmp_path / "test.db")) as store:
        first = {"expected_revision": 0, "configuration": auto(manual("a"))}
        model_roles.save(store, model_roles.Update.model_validate(first))
        changed = model_roles.load(store)["configuration"]
        changed["roleSettings"]["roles"]["answer"].update(eligibleModelIds=["d"], modelId="d")
        second = {"expected_revision": 1, "configuration": changed}
        model_roles.save(store, model_roles.Update.model_validate(second))
        roles = model_roles.load(store)["configuration"]["roleSettings"]["roles"]
    assert roles["summarization"]["modelId"] == "d" and roles["summarization"]["followAnswer"]


def test_auto_role_that_is_off_stays_off():
    value = auto(manual("b"), enabled=False)
    role = model_roles.Configuration.model_validate(value).roleSettings.roles["summarization"]
    assert not role.enabled and role.modelId == "b"
    with pytest.raises(model_roles.SelectionError, match="disabled"):
        model_roles.dispatch(value, "summarization", MESSAGES, {"one": Adapter()})


def test_auto_role_needs_an_answer_model():
    value = auto(manual())
    value["roleSettings"]["roles"]["answer"] = off()
    with pytest.raises(ValidationError, match="enabled primary/fallback"):
        model_roles.Configuration.model_validate(value)


def test_answer_cannot_follow_itself():
    value = manual()
    value["roleSettings"]["roles"]["answer"]["followAnswer"] = True
    with pytest.raises(ValidationError, match="cannot follow itself"):
        model_roles.Configuration.model_validate(value)


def test_saved_settings_without_the_flag_keep_their_models(tmp_path):
    legacy = config()
    with closing(Store(tmp_path / "test.db")) as store:
        with store._connect() as db:
            db.execute("INSERT INTO model_role_settings VALUES(1,1,?)", (json.dumps(legacy),))
        roles = model_roles.load(store)["configuration"]["roleSettings"]["roles"]
    assert roles["summarization"]["modelId"] == "a"
    assert not any(role["followAnswer"] for role in roles.values())


def test_upgrade_adds_later_roles_switched_off():
    value = auto(manual())
    for role in model_roles.LATER_ROLES:
        del value["roleSettings"]["roles"][role]
    roles = model_roles.Configuration.model_validate(value).roleSettings.roles
    assert not any(
        roles[role].enabled or roles[role].followAnswer for role in model_roles.LATER_ROLES
    )
