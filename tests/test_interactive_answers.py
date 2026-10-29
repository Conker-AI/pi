"""Interactive answers: when a turn is taught the dashboard's catalog, and that history stays plain."""

import pytest

from pi.conversation import output_settings
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.models import roles as model_roles
from pi.owner import settings as owner_settings
from pi.turns import prompt_prefix


def configuration(answer_mode="manual", model="claude", eligible=("claude",), **answer):
    roles = {
        role: dict(
            enabled=False,
            eligibleModelIds=[],
            modelId=None,
            timeoutMs=1000,
            failure="stop",
            fallbackModelId=None,
        )
        for role in model_roles.ROLES
    }
    roles["answer"].update(enabled=True, eligibleModelIds=list(eligible), modelId=model, **answer)
    if answer_mode == "router":
        roles["routing"].update(enabled=True, eligibleModelIds=["laya"], modelId="laya")
    value = dict(
        providers=[
            dict(id="ollama", name="Local", enabled=True),
            dict(id="anthropic", name="Anthropic", enabled=True),
            dict(id="decisions", name="Laya", enabled=True),
        ],
        models=[
            dict(id="qwen", name="Qwen", providerId="ollama", route="qwen3:4b", enabled=True),
            dict(id="claude", name="Claude", providerId="anthropic", route="claude", enabled=True),
            dict(id="haiku", name="Haiku", providerId="anthropic", route="haiku", enabled=True),
            dict(
                id="laya", name="Laya", providerId="decisions", route="model-routing", enabled=True
            ),
        ],
        defaultModelId="claude",
        roleSettings=dict(answerMode=answer_mode, roles=roles),
    )
    # Only real configurations: the same validation a saved one passes.
    return model_roles.Configuration.model_validate(value).model_dump()


def execution(choice, models=None, **extra):
    return {
        "output": {**output_settings.AUTO, "interactive": choice},
        "modelConfiguration": models,
        **extra,
    }


def taught(value):
    return any(
        message.content == output_settings.CATALOG for message in output_settings.messages(value)
    )


def test_hosted_providers_are_the_hosted_adapters():
    from pi.models.chatgpt import ChatGPTProvider
    from pi.models.claude_code import ClaudeCodeProvider
    from pi.models.direct import AnthropicProvider, OpenAIProvider
    from pi.models.openrouter import OpenRouterProvider
    from pi.models.providers import OllamaProvider

    hosted = {
        OpenAIProvider,
        AnthropicProvider,
        OpenRouterProvider,
        ChatGPTProvider,
        ClaudeCodeProvider,
    }
    assert {adapter.name for adapter in hosted} == output_settings.HOSTED_PROVIDERS
    assert OllamaProvider.name not in output_settings.HOSTED_PROVIDERS


def test_the_pinned_catalog_is_the_dashboard_prompt():
    catalog = output_settings.CATALOG
    assert catalog.startswith("## Interactive answers")
    for part in ("```openui", "Stack(", "Table(", "Chart(", "Choices(", "Form(", "Callout("):
        assert part in catalog
    for absent in ("Query(", "Mutation(", "@Run", "HtmlView"):
        assert absent not in catalog


@pytest.mark.parametrize(
    ("models", "expected"),
    [
        (configuration(model="claude"), True),
        (configuration(model="qwen", eligible=("qwen",)), False),
        (None, False),
    ],
    ids=["hosted", "local", "legacy-router-local"],
)
def test_auto_teaches_hosted_models_only(models, expected):
    assert taught(execution("auto", models)) is expected


def test_auto_treats_an_unknown_provider_as_not_hosted():
    models = configuration(model="claude")
    models["providers"].append(dict(id="fixture", name="Fixture", enabled=True))
    next(m for m in models["models"] if m["id"] == "claude")["providerId"] = "fixture"
    assert not taught(execution("auto", models))
    assert taught(execution("on", models))


def test_auto_follows_the_model_chosen_for_this_turn():
    local_default = configuration(model="qwen", eligible=("qwen", "claude"))
    assert not taught(execution("auto", local_default))
    assert taught(execution("auto", local_default, answerModelId="claude"))
    assert taught(execution("auto", local_default, configuration={"modelId": "claude"}))
    hosted_default = configuration(model="claude", eligible=("qwen", "claude"))
    assert not taught(execution("auto", hosted_default, answerModelId="qwen"))


def test_auto_with_a_router_needs_every_possible_answer_model_hosted():
    hosted = configuration("router", eligible=("claude", "haiku"))
    mixed = configuration("router", eligible=("claude", "qwen"))
    assert taught(execution("auto", hosted))
    assert not taught(execution("auto", mixed)), "the router may pick the local model"
    disabled = configuration("router", eligible=("claude", "qwen"))
    next(m for m in disabled["models"] if m["id"] == "qwen")["enabled"] = False
    assert taught(execution("auto", disabled)), "a disabled local model cannot answer"


@pytest.mark.parametrize(
    "models", [configuration(model="claude"), configuration(model="qwen", eligible=("qwen",)), None]
)
def test_on_and_off_ignore_the_model(models):
    assert taught(execution("on", models))
    assert not taught(execution("off", models))
    assert not taught({"output": dict(output_settings.AUTO), "modelConfiguration": models})


def test_calls_and_json_answers_are_never_taught():
    hosted = configuration(model="claude")
    assert not taught(execution("on", hosted, callExecution={"id": "call", "generation": 1}))
    value = execution("on", hosted)
    value["output"]["format"] = "json"
    assert not taught(value)


def test_the_catalog_comes_before_the_owners_format_settings():
    value = execution("on", configuration())
    value["output"].update(format="table", length="short")
    first, second = output_settings.messages(value)
    assert first.content == output_settings.CATALOG
    assert second.content.startswith("Owner output settings for this chat.")


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "pi.db")
    yield value
    value.close()


def owner_choice(store, choice):
    revision = owner_settings.load(store)["revision"]
    owner_settings.save(
        store,
        owner_settings.Update(
            expected_revision=revision, values={output_settings.INTERACTIVE_SETTING: choice}
        ),
    )


def test_each_turn_snapshot_carries_the_owners_choice(store):
    session = store.create_session()
    assert session_settings.execution(store, session)["output"]["interactive"] == "auto"
    owner_choice(store, "on")
    assert session_settings.execution(store, session)["output"]["interactive"] == "on"
    owner_choice(store, "off")
    assert session_settings.execution(store, session).get("output") is None
    owner_choice(store, "sometimes")
    assert session_settings.execution(store, session)["output"]["interactive"] == "auto"


def test_the_turn_prefix_teaches_the_catalog_and_history_keeps_the_raw_answer(store):
    session = store.create_session()
    owner_choice(store, "on")
    value = session_settings.execution(store, session)
    prefix = prompt_prefix.system_messages(store, "base prompt", value, None, None)
    assert [m.content for m in prefix].count(output_settings.CATALOG) == 1
    owner_choice(store, "auto")
    value = session_settings.execution(store, session)
    prefix = prompt_prefix.system_messages(store, "base prompt", value, None, None)
    assert output_settings.CATALOG not in [m.content for m in prefix], "no model configured: local"
    answer = 'Here:\n\n```openui\nroot = Stack([Text("hi")])\n```'
    message = store.append_message(session, "assistant", answer)
    assert [row["content"] for row in store.messages(session) if row["id"] == message["id"]] == [
        answer
    ]
