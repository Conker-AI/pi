"""Answer effort: the chat setting, what the turn snapshot carries, which call it reaches, and what
each provider is asked. A model with no control ignores it and the settings reply says so."""

import json
from contextlib import closing
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from test_suggestions import config

from pi.conversation import output_settings
from pi.conversation.store import Store
from pi.models import effort
from pi.models import providers as provider_module
from pi.models import roles as model_roles
from pi.models.direct import AnthropicProvider, GeminiProvider, OpenAIProvider
from pi.models.openrouter import OpenRouterProvider
from pi.models.providers import Completion, Message, OllamaProvider
from pi.turns.model_calls import answer_effort


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def chat_with(store, **output):
    sid = store.create_session()
    update = output_settings.Update(expected_revision=0, output=output_settings.Output(**output))
    output_settings.save(store, sid, update)
    return sid


def snapshot(store, sid):
    with store._connect() as db:
        return output_settings.snapshot(db, sid)


def asked(level):
    return provider_module.effort_scope(level)


# --- the chat setting -----------------------------------------------------------------------


def test_effort_defaults_to_auto_and_only_the_four_levels_are_accepted(store):
    sid = store.create_session()
    assert output_settings.load(store, sid)["output"]["effort"] == "auto"
    for level in ("light", "medium", "high", "max", "auto"):
        assert output_settings.Output(effort=level).effort == level
    for bad in ("xhigh", "low", "", "HIGH", None, 3):
        with pytest.raises(ValidationError):
            output_settings.Output(effort=bad)


def test_effort_is_saved_per_chat_and_loaded_back(store):
    sid = chat_with(store, effort="high")
    other = store.create_session()
    assert output_settings.load(store, sid)["output"]["effort"] == "high"
    assert output_settings.load(store, other)["output"]["effort"] == "auto"


def test_a_row_saved_before_effort_existed_loads_as_auto(store):
    sid = store.create_session()
    with store._connect() as db:
        db.execute(
            "INSERT INTO session_output VALUES (?,?,?)",
            (sid, 1, json.dumps({"format": "auto", "length": "auto", "language": "auto"})),
        )
    assert output_settings.load(store, sid)["output"]["effort"] == "auto"


def test_a_fork_keeps_the_chats_effort(store):
    parent = chat_with(store, effort="max")
    with store._connect() as db:
        db.execute(
            "INSERT INTO sessions (id, parent_id, created_at) VALUES (?,?,?)",
            ("ses_child", parent, 2),
        )
    assert output_settings.load(store, "ses_child")["output"]["effort"] == "max"


def test_the_turn_snapshot_carries_effort_only_when_chosen(store):
    assert snapshot(store, store.create_session()) is None
    assert snapshot(store, chat_with(store, effort="auto")) is None
    chosen = snapshot(store, chat_with(store, effort="light"))
    assert chosen is not None and chosen["effort"] == "light"
    mixed = snapshot(store, chat_with(store, format="table", effort="medium"))
    assert mixed is not None and mixed["format"] == "table" and mixed["effort"] == "medium"
    plain = snapshot(store, chat_with(store, format="table"))
    assert plain is not None and "effort" not in plain


def test_effort_is_not_prompt_text():
    assert output_settings.messages({"output": {"effort": "max"}}) == []
    only = output_settings.messages({"output": {"effort": "max", "length": "short"}})
    assert "max" not in only[-1].content and "effort" not in only[-1].content.lower()


# --- which call carries it --------------------------------------------------------------------


def test_effort_for_the_answer_call_comes_from_the_frozen_snapshot():
    assert answer_effort({"output": {"effort": "high"}}) == "high"
    assert answer_effort({"output": {}}) is None
    assert answer_effort({}) is None
    assert answer_effort({"output": {"effort": "bogus"}}) is None


def test_a_call_a_team_role_and_an_agent_run_ignore_effort():
    execution = {"output": {"effort": "max"}}
    assert answer_effort({**execution, "callExecution": {"x": 1}}) is None
    assert answer_effort({**execution, "kind": "team-role"}) is None


class Seen:
    """An adapter that records the effort in force while it answers."""

    name = "seen"

    def __init__(self, record):
        self.record = record

    def complete_bounded(self, messages, *, model, timeout):
        self.record.append((model, provider_module.requested_effort()))
        return Completion(text="ok", model=model, provider=self.name)


def dispatch(role, level):
    record = []
    value = config()
    value["roleSettings"]["roles"]["thinking"] = {
        **value["roleSettings"]["roles"]["answer"],
        "enabled": True,
    }
    adapters = {"one": Seen(record), "two": Seen(record)}
    model_roles.dispatch(value, role, [Message("user", "Hi")], adapters, effort=level)
    return record


def test_the_answer_call_is_asked_for_effort_and_nothing_leaks_after():
    assert dispatch("answer", "high") == [("actual-a", "high")]
    assert provider_module.requested_effort() is None


def test_no_effort_means_the_call_is_asked_for_none():
    assert dispatch("answer", None) == [("actual-a", None)]


def test_another_role_is_never_asked_for_effort():
    assert dispatch("thinking", "max") == [("actual-a", None)]


def router_config():
    value = config()
    roles = value["roleSettings"]["roles"]
    roles["routing"] = {**roles["answer"], "enabled": True, "eligibleModelIds": ["a"]}
    value["roleSettings"]["answerMode"] = "router"
    return value


def test_the_routing_helper_inside_a_routed_answer_is_not_asked():
    record = []

    class Router(Seen):
        def complete_bounded(self, messages, *, model, timeout):
            self.record.append((model, provider_module.requested_effort()))
            return Completion(text='{"modelId": "b"}', model=model, provider=self.name)

    adapters = {"one": Router(record), "two": Router(record)}
    model_roles.dispatch(router_config(), "answer", [Message("user", "Hi")], adapters, effort="max")
    # The routing helper is asked first (model a, no effort); the answer it picked gets max.
    assert record[0] == ("actual-a", None)
    assert ("actual-b", "max") in record


# --- what each provider is asked ----------------------------------------------------------------


def test_openai_reasoning_models_get_reasoning_effort():
    provider = OpenAIProvider("k", allow_paid=True)
    messages = [Message("user", "Hi")]
    for route, level, sent in (
        ("gpt-5", "light", "low"),
        ("gpt-5.2", "medium", "medium"),
        ("o3", "high", "high"),
        ("o4-mini", "max", "high"),
    ):
        with asked(level):
            assert provider._payload(messages, route)["reasoning_effort"] == sent


def test_openai_models_without_reasoning_and_no_effort_send_nothing():
    provider = OpenAIProvider("k", allow_paid=True)
    messages = [Message("user", "Hi")]
    with asked("high"):
        for route in ("gpt-4o", "gpt-5-chat-latest", "my-o3"):
            assert "reasoning_effort" not in provider._payload(messages, route)
    assert "reasoning_effort" not in provider._payload(messages, "gpt-5")


def test_other_openai_compatible_vendors_ignore_it():
    provider = GeminiProvider("k", allow_paid=True)
    with asked("high"):
        assert "reasoning_effort" not in provider._payload([Message("user", "Hi")], "o3")


def capabilities(monkeypatch, body, calls=None, status=200):
    effort._capabilities.clear()

    def fake(url, **options):
        if calls is not None:
            calls.append(url)
        return httpx.Response(status, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(effort.httpx, "get", fake)


LEVELS_OK = {
    "capabilities": {
        "effort": {
            "supported": True,
            "low": {"supported": True},
            "medium": {"supported": True},
            "high": {"supported": True},
            "max": {"supported": False},
        }
    }
}


def test_anthropic_sends_output_config_from_the_models_own_capability_list(monkeypatch):
    calls = []
    capabilities(monkeypatch, LEVELS_OK, calls)
    provider = AnthropicProvider("k", allow_paid=True)
    messages = [Message("user", "Hi")]
    with asked("medium"):
        assert provider._payload(messages, "claude-x")["output_config"] == {"effort": "medium"}
    with asked("max"):  # not supported by this model: the next best it does support
        assert provider._payload(messages, "claude-x")["output_config"] == {"effort": "high"}
    assert calls == ["https://api.anthropic.com/v1/models/claude-x"]  # read once, then remembered


def test_anthropic_without_the_capability_or_an_answer_sends_nothing(monkeypatch):
    provider = AnthropicProvider("k", allow_paid=True)
    messages = [Message("user", "Hi")]
    for body, status in (
        ({"capabilities": {"effort": {"supported": False}}}, 200),
        ({"capabilities": {}}, 200),
        ({"error": "x"}, 404),
        ({"capabilities": {"effort": {"supported": True}}}, 200),
    ):
        capabilities(monkeypatch, body, status=status)
        with asked("high"):
            assert "output_config" not in provider._payload(messages, "claude-y")


def test_anthropic_asks_nothing_when_no_effort_was_chosen(monkeypatch):
    calls = []
    capabilities(monkeypatch, LEVELS_OK, calls)
    AnthropicProvider("k", allow_paid=True)._payload([Message("user", "Hi")], "claude-x")
    assert calls == []


def test_a_route_that_is_not_a_model_id_is_never_put_in_the_capability_url(monkeypatch):
    calls = []
    capabilities(monkeypatch, LEVELS_OK, calls)
    provider = AnthropicProvider("k", allow_paid=True)
    with asked("high"):
        for route in ("../../x", "a b", "a/b", "x?y=1"):
            assert "output_config" not in provider._payload([Message("user", "Hi")], route)
    assert calls == []


def test_openrouter_sends_reasoning_only_for_a_model_that_lists_it(monkeypatch):
    sent = []
    provider = OpenRouterProvider("k", allow_paid=True)

    def post(url, json, **options):
        sent.append(json)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "model": "m"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("pi.models.openrouter.httpx.post", post)

    def run(reasoning, level):
        provider._guard_cost = lambda model: SimpleNamespace(
            supports_images=False, supports_reasoning=reasoning
        )
        with asked(level):
            provider.complete_bounded([Message("user", "Hi")], model="m", timeout=5)
        return sent[-1].get("reasoning")

    assert run(True, "max") == {"effort": "xhigh"}
    assert run(True, "light") == {"effort": "low"}
    assert run(False, "high") is None
    assert run(True, None) is None


def test_ollama_takes_a_level_only_for_gpt_oss(monkeypatch):
    for route, level, expected in (
        ("gpt-oss:20b", "light", "low"),
        ("gpt-oss:20b", "max", "high"),
        ("qwen3:4b", "high", None),
    ):
        assert effort.ollama_value(route, level) == expected
    assert effort.ollama_value("gpt-oss:20b", None) is None
    captured = []

    def post(url, json, **options):
        captured.append(json)
        return httpx.Response(
            200,
            json={"message": {"content": "ok"}, "done": True, "model": json["model"]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("pi.models.providers.httpx.post", post)
    provider = OllamaProvider("http://127.0.0.1:11434")
    with asked("light"):
        provider.complete_bounded([Message("user", "Hi")], model="gpt-oss:20b", timeout=5)
        provider.complete_bounded([Message("user", "Hi")], model="qwen3:4b", timeout=5)
    assert captured[0]["think"] == "low"
    assert "think" not in captured[1]


# --- what the settings reply says ---------------------------------------------------------------


def test_the_settings_reply_says_which_models_ignore_effort(store):
    value = router_config()
    value["providers"] = [
        dict(id="openai", name="OpenAI", enabled=True),
        dict(id="gemini", name="Gemini", enabled=True),
    ]
    value["models"][0].update(providerId="openai", route="gpt-5")
    value["models"][1].update(providerId="gemini", route="gemini-3")
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(value)
        ),
    )
    sid = store.create_session()
    support = output_settings.load(store, sid)["effortSupport"]
    assert support == [
        {"modelId": "a", "providerId": "openai", "applies": True},
        {"modelId": "b", "providerId": "gemini", "applies": False},
    ]
    saved = output_settings.save(
        store,
        sid,
        output_settings.Update(expected_revision=0, output=output_settings.Output(effort="high")),
    )
    assert saved["effortSupport"] == support


def test_support_is_unknown_where_pi_only_learns_it_from_the_provider():
    assert effort.applies("anthropic", "claude-x") is None
    assert effort.applies("openrouter", "vendor/model") is None
    assert effort.applies("openai", "gpt-4o") is False
    assert effort.applies("claude-code", "haiku") is False
    assert effort.applies("chatgpt", "gpt-5") is False
    assert effort.applies("ollama", "gpt-oss:20b") is True


def test_no_configured_models_means_no_support_list(store):
    assert output_settings.load(store, store.create_session())["effortSupport"] == []


# --- a real turn ----------------------------------------------------------------------------------


def test_a_turn_asks_the_answer_model_for_the_chats_effort(store):
    from pi.models.routing import Router
    from pi.turns.loop import Loop

    model_roles.save(store, model_roles.Update(expected_revision=0, configuration=config()))
    record: list = []

    class Provider(Seen):
        name = "one"

    loop = Loop(store, Router(local_provider=Provider(record), local_model="actual-a"))
    chosen = chat_with(store, effort="high")
    plain = store.create_session()
    loop.run_turn(chosen, "Compare these choices")
    loop.run_turn(plain, "Compare these choices")
    assert record == [("actual-a", "high"), ("actual-a", None)]
    assert provider_module.requested_effort() is None


def test_o1_mini_and_o1_preview_are_not_sent_a_level_they_refuse():
    for route in ("o1-mini", "o1-preview"):
        assert effort.openai_value(route, "high") is None
        assert effort.applies("openai", route) is False
    assert effort.openai_value("o1", "high") == "high"
    assert effort.openai_value("o3-mini", "light") == "low"


def test_without_effort_leaves_the_rest_of_the_snapshot_alone():
    from pi.turns.model_calls import without_effort

    value = {"kind": "companion", "output": {"format": "list", "effort": "max"}}
    stripped = without_effort(value)
    assert stripped == {"kind": "companion", "output": {"format": "list"}}
    assert answer_effort(stripped) is None
    assert value["output"]["effort"] == "max", "the frozen snapshot is not changed"
    plain = {"output": {"format": "list"}}
    assert without_effort(plain) is plain and without_effort({}) == {}


def test_the_deep_research_plan_call_is_not_asked_for_the_chats_effort(store):
    from test_deep_research import FOLLOWUP, PLAN
    from test_web_research import SEARCH, SearchGate

    from pi.models.routing import Router
    from pi.turns.loop import Loop

    model_roles.save(store, model_roles.Update(expected_revision=0, configuration=config()))
    record: list = []
    replies = iter([PLAN, SEARCH, FOLLOWUP, "Synthesis"])

    class Provider(Seen):
        name = "one"

        def complete_bounded(self, messages, *, model, timeout):
            self.record.append((model, provider_module.requested_effort()))
            return Completion(text=next(replies), model=model, provider=self.name)

    loop = Loop(
        store,
        Router(local_provider=Provider(record), local_model="actual-a"),
        toolgate=SearchGate(),
    )
    loop.run_turn(chat_with(store, effort="max"), "Investigate", research_mode="deep")
    assert record[0] == ("actual-a", None), "the plan is not the answer"
    assert ("actual-a", "max") in record[1:], "the answer itself still is"


def test_the_support_list_follows_the_model_an_agent_pins_for_the_chat(store):
    value = router_config()
    value["providers"] = [
        dict(id="openai", name="OpenAI", enabled=True),
        dict(id="gemini", name="Gemini", enabled=True),
    ]
    value["models"][0].update(providerId="openai", route="gpt-5")
    value["models"][1].update(providerId="gemini", route="gemini-3")
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(value)
        ),
    )
    sid = store.create_session()
    pinned = {"configuration": {"modelId": "b"}}
    from pi.conversation import settings as session_settings

    real = session_settings.execution
    session_settings.execution = lambda _store, _id, *a, **k: {**real(_store, _id), **pinned}
    try:
        support = output_settings.load(store, sid)["effortSupport"]
    finally:
        session_settings.execution = real
    assert support == [{"modelId": "b", "providerId": "gemini", "applies": False}]
