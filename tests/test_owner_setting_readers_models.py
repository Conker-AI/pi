"""Owner model settings: the models each Auto mode offers, Think, usual complexity, and who judges
answers and checks facts. Each test fails if its reader ignores the value (round four of R-F1)."""

import json
from contextlib import closing

import pytest

from pi.conversation import fact_checks, judgements
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.core import owner_settings as owner_settings
from pi.models import auto_settings, owner_choices
from pi.models import roles as model_roles
from pi.models.providers import Message
from tests.test_auto_settings import VALID, Recording, configured
from tests.test_fact_checks import CLAIMS_JSON, create_researched_answer
from tests.test_fact_checks import Recording as CheckRecording
from tests.test_fact_checks import setup_models as check_models
from tests.test_judgements import JUDGE_JSON, create_family
from tests.test_judgements import setup_models as judge_models
from tests.test_model_roles import config
from tests.test_response_retries import Recording as JudgeRecording


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def choose(store, values):
    revision = owner_settings.load(store)["revision"]
    owner_settings.save(store, owner_settings.Update(expected_revision=revision, values=values))


def propose(store, reply=VALID, mode="auto"):
    seen = Recording(json.dumps(reply))
    result = auto_settings.propose(
        store,
        {"one": seen},
        auto_settings.Request(text="Best laptop under $1000?", mode=mode),
        source_privacy=session_settings.source_privacy,
    )
    return result, json.loads(seen.messages[0][1].content)


# models.mode_fast, models.mode_balanced, models.mode_deep


def test_a_mode_offers_only_the_owners_models_for_it_in_his_order(store):
    configured(store)
    choose(store, {"models.mode_deep": ["b", "gone", "a"], "models.mode_fast": ["a"]})
    _, sent = propose(store, mode="deep")
    assert [model["id"] for model in sent["models"]] == ["b", "a"]
    # One model is no choice for the router: the proposal names it.
    result, sent = propose(store, mode="fast")
    assert "models" not in sent and result["answerModelId"] == "a"
    assert result["reasons"]["model"] == "The only model you chose for Fast mode."
    # A mode without a list, or Auto, offers every answer model as before.
    for mode in ("balanced", "auto"):
        result, sent = propose(store, mode=mode)
        assert [model["id"] for model in sent["models"]] == ["a", "b"]
        assert "answerModelId" not in result


def test_a_mode_list_of_unusable_models_offers_every_model(store):
    configured(store)
    choose(store, {"models.mode_balanced": ["gone", 5, None], "models.mode_deep": "b"})
    for mode in ("balanced", "deep"):
        _, sent = propose(store, mode=mode)
        assert [model["id"] for model in sent["models"]] == ["a", "b"]


# models.think


def test_the_owners_think_setting_decides_autos_thinking(store):
    configured(store)
    assert propose(store)[0]["think"] is True, "Auto leaves it to the routing model"
    choose(store, {"models.think": "off"})
    result, _ = propose(store)
    assert result["think"] is False and result["reasons"]["think"] == "Your Think setting is off."
    choose(store, {"models.think": "on"})
    assert propose(store, {**VALID, "think": False})[0]["think"] is True
    choose(store, {"models.think": "sometimes"})
    assert propose(store, {**VALID, "think": False})[0]["think"] is False


# models.complexity


def test_the_routing_model_hears_how_hard_requests_usually_are(store):
    configured(store)
    assert "usualComplexity" not in propose(store)[1]
    choose(store, {"models.complexity": "high"})
    assert propose(store)[1]["usualComplexity"] == "high"
    choose(store, {"models.complexity": "extreme"})
    assert "usualComplexity" not in propose(store)[1]


def test_the_answers_router_gets_the_usual_complexity_as_an_owner_hint(store):
    value = config()
    value["roleSettings"]["answerMode"] = "router"
    value["roleSettings"]["roles"]["routing"] = {**value["roleSettings"]["roles"]["answer"]}
    router, answer = Recording('{"modelId":"b"}'), Recording("answer")
    task = [Message("user", "Compare these laptops")]
    for chosen, hints, expected in [
        ({}, None, None),
        ({"models.complexity": "low"}, None, {"usualComplexity": "low"}),
        (
            {"models.complexity": "medium"},
            {"complexity": "hard"},
            {"complexity": "hard", "usualComplexity": "medium"},
        ),
    ]:
        choose(store, chosen)
        router.messages.clear()
        model_roles.dispatch(
            value,
            "answer",
            task,
            {"one": router, "two": answer},
            hints=owner_choices.routing_hints(store, hints),
        )
        assert json.loads(router.messages[0][1].content).get("ownerHints") == expected


# models.jobs.judge


def judged(store, request_id):
    adapters = {"one": JudgeRecording(JUDGE_JSON), "two": JudgeRecording(JUDGE_JSON)}
    from pi.models.routing import Router
    from pi.turns.loop import Loop

    sid = store.create_session()
    root, _, _ = create_family(store, Loop(store, Router(providers=adapters)), sid)
    body = judgements.Judge(request_id=request_id, rubric="Test rubric")
    return judgements.judge(store, adapters, sid, root, body), root


def test_the_owners_first_usable_judge_judges_when_the_request_names_none(store):
    judge_models(store)
    result, root = judged(store, "judge_request_101")
    assert result["judgeModelId"] == "a", "the routing model, as before"
    choose(store, {"models.jobs.judge": ["gone", "b", "a"]})
    result, root = judged(store, "judge_request_102")
    assert result["judgeModelId"] == "b"
    assert root not in result["excluded"] and len(result["excluded"]) == 1


# models.jobs.fact_check


def checked(store, request_id):
    adapters = {"one": CheckRecording(CLAIMS_JSON), "two": CheckRecording(CLAIMS_JSON)}
    sid = store.create_session()
    mid, _ = create_researched_answer(store, sid)
    return fact_checks.check(store, adapters, sid, mid, fact_checks.Check(request_id=request_id))


def test_the_owners_first_usable_fact_checker_checks_when_the_request_names_none(store):
    check_models(store)
    assert checked(store, "factcheck_request_101")["modelId"] == "a"
    choose(store, {"models.jobs.fact_check": ["b"]})
    assert checked(store, "factcheck_request_102")["modelId"] == "b"
    choose(store, {"models.jobs.fact_check": ["gone"]})
    assert checked(store, "factcheck_request_103")["modelId"] == "a"
