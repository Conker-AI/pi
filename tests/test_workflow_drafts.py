"""Workflow drafts: the talking model drafts an editor workflow for review; nothing is saved."""

import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.agents import workflow_drafts
from pi.api import server
from pi.conversation.store import Store
from pi.models import roles as model_roles
from tests.test_model_roles import Adapter, config

CAPABILITIES = [
    {
        "id": "weather-now",
        "kind": "tool",
        "name": "Weather now",
        "description": "Current weather for a city",
        "inputs": [{"name": "city", "type": "string", "required": True}],
    },
    {"id": "daily-brief", "kind": "workflow", "name": "Daily brief", "version": 3},
]
DOCUMENT = {
    "name": "Morning weather",
    "description": "Reads the weather for a city.",
    "inputs": [{"name": "city", "type": "string", "required": True}],
    "outputs": [{"name": "report", "type": "object", "required": True}],
    "nodes": [
        {"id": "start", "type": "input", "label": "Start", "config": {}},
        {
            "id": "fetch",
            "type": "tool_call",
            "label": "Fetch weather",
            "config": {"tool": "weather-now", "args": {"city": "$input.city"}},
        },
        {
            "id": "brief",
            "type": "workflow_call",
            "label": "Brief",
            "config": {"toolId": "daily-brief", "version": 1, "args": {}},
        },
        {"id": "done", "type": "return", "label": "Done", "config": {"value": "$last"}},
    ],
    "edges": [
        {"id": "e1", "source": "start", "target": "fetch"},
        {"source": "fetch", "target": "brief"},
        {"id": "e3", "source": "brief", "target": "done"},
    ],
    "effect": "read",
    "credentialRefs": ["connection:bank"],
    "agentVisible": True,
    "budgets": {"maxSteps": 500, "maxLoopItems": 5},
}


class Recording(Adapter):
    def __init__(self, response, fail=False):
        super().__init__(response, fail=fail)
        self.messages = []

    def complete_bounded(self, messages, *, model, timeout):
        self.messages, self.timeout = messages, timeout
        return super().complete_bounded(messages, model=model, timeout=timeout)


def configured(store):
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(config())
        ),
    )


def request(**values):
    return workflow_drafts.Request.model_validate(
        {"description": "Tell me the weather in Tel Aviv", "capabilities": CAPABILITIES, **values}
    )


def drafted(tmp_path, reply, **values):
    adapter = Recording(reply if isinstance(reply, str) else json.dumps(reply))
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        return workflow_drafts.draft(store, {"one": adapter}, request(**values)), adapter


def test_ready_draft_takes_the_editor_shape_with_pi_owned_fields(tmp_path):
    result, adapter = drafted(
        tmp_path,
        {"reply": "Reads the weather.", "ready": True, "document": DOCUMENT},
        draftId="tlv",
    )
    document = result["document"]
    assert result["ready"] is True and result["issues"] == []
    assert result["modelId"] == "a" and result["configurationRevision"] == 1
    assert document["id"] == "tlv" and document["kind"] == "workflow"
    # The model cannot bind credentials or show the draft to agents; the owner decides that.
    assert document["credentialRefs"] == [] and document["agentVisible"] is False
    assert [node["position"] for node in document["nodes"]] == [
        {"x": 0.0, "y": 0.0},
        {"x": 0.0, "y": 120.0},
        {"x": 0.0, "y": 240.0},
        {"x": 0.0, "y": 360.0},
    ]
    assert document["nodes"][2]["config"]["version"] == 3
    assert [edge["id"] for edge in document["edges"]] == ["e1", "e2", "e3"]
    assert document["budgets"] == {"maxSteps": 50, "maxLoopItems": 5, "timeoutMs": 15000}
    assert document["effect"] == "read"
    assert len(adapter.calls) == 1


def test_the_model_sees_only_the_request_as_data(tmp_path):
    _, adapter = drafted(
        tmp_path,
        {"reply": "Which city?", "ready": False, "document": None},
        conversation=[{"role": "builder", "content": "Which city?"}],
        issues=["Use exactly one Input node."],
    )
    system, user = adapter.messages
    assert system.role == "system" and "untrusted data" in system.content
    assert json.loads(user.content).keys() == {
        "description",
        "conversation",
        "capabilities",
        "previous",
        "issues",
    }


def test_a_question_returns_no_document(tmp_path):
    result, _ = drafted(
        tmp_path,
        '```json\n{"reply": "Which city?", "ready": false, "document": {"name": "x"}}\n```',
    )
    assert result == {
        "reply": "Which city?",
        "ready": False,
        "document": None,
        "issues": [],
        "modelId": "a",
        "configurationRevision": 1,
    }


def test_capabilities_the_owner_did_not_supply_are_issues(tmp_path):
    document = json.loads(json.dumps(DOCUMENT))
    document["nodes"][1]["config"]["tool"] = "bank-transfer"
    document["nodes"][2]["config"]["toolId"] = "unknown-flow"
    result, _ = drafted(tmp_path, {"reply": "Done.", "ready": True, "document": document})
    assert result["document"]["id"] == "morning-weather"
    assert result["issues"] == [
        "Step fetch uses 'bank-transfer', which is not an available tool.",
        "Step brief uses 'unknown-flow', which is not a published workflow.",
    ]


@pytest.mark.parametrize(
    "reply",
    [
        "I would rather not.",
        json.dumps({"reply": "Ready", "ready": True, "document": None}),
        json.dumps({"reply": "Ready", "ready": True, "document": {**DOCUMENT, "nodes": []}}),
        json.dumps(
            {
                "reply": "Ready",
                "ready": True,
                "document": {**DOCUMENT, "edges": [{"source": "start", "target": "x y"}]},
            }
        ),
        json.dumps(
            {
                "reply": "Ready",
                "ready": True,
                "document": {**DOCUMENT, "nodes": [{"id": "a", "type": "shell"}] * 2},
            }
        ),
    ],
)
def test_unusable_model_output_is_refused(tmp_path, reply):
    with pytest.raises(workflow_drafts.DraftError) as raised:
        drafted(tmp_path, reply)
    assert raised.value.status == 502 and raised.value.code == "unusable"


def test_needs_a_talking_model(tmp_path):
    with (
        closing(Store(tmp_path / "pi.db")) as store,
        pytest.raises(workflow_drafts.DraftError) as raised,
    ):
        workflow_drafts.draft(store, {}, request())
    assert raised.value.code == "not_configured" and raised.value.status == 503


def test_an_unavailable_model_is_reported(tmp_path):
    adapter = Recording("", fail=True)
    with (
        closing(Store(tmp_path / "pi.db")) as store,
        pytest.raises(workflow_drafts.DraftError) as raised,
    ):
        configured(store)
        workflow_drafts.draft(store, {"one": adapter}, request())
    assert raised.value.code == "unavailable"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Morning weather!", "morning-weather"),
        ("2 day plan", "workflow-2-day-plan"),
        ("???", "workflow"),
    ],
)
def test_draft_ids_come_from_the_name(name, expected):
    assert workflow_drafts.slug(name) == expected


def test_http_route_needs_the_admin_key(tmp_path):
    adapter = Recording(json.dumps({"reply": "Which city?", "ready": False, "document": None}))
    with closing(Store(tmp_path / "pi.db")) as store:
        configured(store)
        app = server.app
        app.state.store = store
        app.state.admin_key = "test-admin-key"
        app.state.router = type("Router", (), {"adapters": lambda self: {"one": adapter}})()
        client = TestClient(app)
        body = {"description": "Weather", "capabilities": CAPABILITIES}
        response = client.post(
            "/workflow-drafts/draft", json=body, headers={"X-Pi-Key": "test-admin-key"}
        )
        assert response.status_code == 200 and response.json()["reply"] == "Which city?"
        owner = client.post(
            "/workflow-drafts/draft", json=body, headers={"X-Pi-Owner-Key": "test-owner-key"}
        )
        assert owner.status_code == 401
        assert client.post("/workflow-drafts/draft", json=body).status_code == 401


def test_the_prompts_example_is_a_complete_valid_draft():
    text = workflow_drafts.INSTRUCTIONS
    example = json.loads(text[text.rindex('{"reply"') : text.index('\nWhen "issues"')])
    capability = workflow_drafts.Capability(id="web.lookup", kind="tool", name="Lookup")
    document, issues = workflow_drafts.normalize(example["document"], [capability], "example")
    assert issues == [] and example["ready"] is True
    assert [node["type"] for node in document["nodes"]] == ["input", "tool_call", "return"]


def test_terminal_control_characters_never_reach_the_reply(tmp_path):
    reply = "\x1b]52;c;cGF5bG9hZA==\x07Which \x9bcity?\x1b[2J\nOne line."
    result, _ = drafted(tmp_path, {"reply": reply, "ready": False, "document": None})
    assert result["reply"] == "]52;c;cGF5bG9hZA==Which city?[2J\nOne line."


def test_a_request_over_one_megabyte_is_refused():
    huge = [
        {
            **CAPABILITIES[0],
            "id": f"tool-{index}",
            "description": "d" * 800,
            "inputs": [{"name": f"i{n}", "description": "x" * 500} for n in range(20)],
        }
        for index in range(120)
    ]
    with pytest.raises(ValueError, match="under 1 MB"):
        request(capabilities=huge)


def test_the_builder_waits_longer_than_an_answer(tmp_path):
    # A slow local model needs minutes to write a whole draft; chat answers keep their limit.
    result, adapter = drafted(tmp_path, {"reply": "Which city?", "ready": False, "document": None})
    assert result["ready"] is False
    assert adapter.timeout == workflow_drafts.MODEL_WAIT_MS / 1000 == 600


def test_a_drafting_model_of_its_own_drafts_while_chat_stays_put(tmp_path):
    settings = config()
    settings["roleSettings"]["roles"]["drafting"] = {
        **settings["roleSettings"]["roles"]["drafting"],
        "enabled": True,
        "eligibleModelIds": ["b"],
        "modelId": "b",
    }
    local = Recording(json.dumps({"reply": "Which city?", "ready": False, "document": None}))
    strong = Recording(json.dumps({"reply": "Which city?", "ready": False, "document": None}))
    with closing(Store(tmp_path / "pi.db")) as store:
        model_roles.save(
            store,
            model_roles.Update(
                expected_revision=0,
                configuration=model_roles.Configuration.model_validate(settings),
            ),
        )
        result = workflow_drafts.draft(store, {"one": local, "two": strong}, request())
    assert result["modelId"] == "b" and strong.messages and not local.messages
    assert strong.timeout == 600


def test_older_settings_gain_drafting_switched_off(tmp_path):
    settings = config()
    del settings["roleSettings"]["roles"]["drafting"]
    loaded = model_roles.Configuration.model_validate(settings)
    assert loaded.roleSettings.roles["drafting"].enabled is False
    adapter = Recording(json.dumps({"reply": "Which city?", "ready": False, "document": None}))
    with closing(Store(tmp_path / "pi.db")) as store:
        model_roles.save(store, model_roles.Update(expected_revision=0, configuration=loaded))
        assert workflow_drafts.draft(store, {"one": adapter}, request())["modelId"] == "a"
