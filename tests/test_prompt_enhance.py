"""Prompt enhance (composer wand): what the model is sent, what comes back, privacy, the daily
cap, junk replies, and the route's keys. Nothing is saved except the cap's own ledger row."""

import hashlib
import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from test_spending_cap import Paid, set_cap
from test_suggestions import Recording, config

from pi.agents import prompt_enhance
from pi.api import server as api
from pi.api.browser_contract import owner_allowed, runtime_allowed, session_only_write
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.models import inference_control, spending_cap
from pi.models import roles as model_roles
from pi.models.providers import Completion, ProviderUnavailable
from pi.owner import settings as owner_settings

ADMIN = "synthetic-admin"
OWNER = "prompt_enhance_test_owner_key"
DRAFT = "write me something about dogs for my blog, make it friendly, keep it short"
BETTER = "Write a short, friendly blog post about dogs."
GOOD = json.dumps({"text": BETTER, "notes": ["Added a goal."]})


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def save(store, value):
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=0, configuration=model_roles.Configuration.model_validate(value)
        ),
    )


def configured(store, *, drafting=False):
    value = config()
    roles = value["roleSettings"]["roles"]
    if drafting:
        roles["drafting"] = {**roles["answer"], "modelId": "b", "eligibleModelIds": ["b"]}
    save(store, value)


def run(store, adapters, text=DRAFT, **extra):
    return prompt_enhance.enhance(store, adapters, prompt_enhance.Request(text=text, **extra))


def tables(store):
    with store._connect() as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {n: db.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


def test_the_model_is_sent_only_the_instructions_and_the_draft(store):
    adapter = Recording(GOOD)
    configured(store)
    chat = store.create_session()
    store.append_message(chat, "user", "my secret earlier message about taxes")
    result = run(store, {"one": adapter})
    assert result == {
        "text": BETTER,
        "notes": ["Added a goal."],
        "modelId": "a",
        "templateId": None,
        "fields": {},
    }
    [messages] = adapter.messages
    assert [m.role for m in messages] == ["system", "user"]
    assert messages[0].content == prompt_enhance.INSTRUCTIONS
    assert json.loads(messages[1].content) == {"text": DRAFT}
    assert "taxes" not in json.dumps([m.content for m in messages])


def test_a_drafting_model_is_used_when_set_and_the_answer_model_otherwise(store):
    configured(store, drafting=True)
    one, two = Recording(GOOD), Recording(GOOD)
    assert run(store, {"one": one, "two": two})["modelId"] == "b"
    assert one.calls == [] and two.calls != []


def test_nothing_is_saved(store):
    configured(store)
    before = tables(store)
    run(store, {"one": Recording(GOOD)})
    assert tables(store) == before


@pytest.mark.parametrize("missing", ["none", "answer off"])
def test_no_model_configured_is_refused_before_any_call(store, missing):
    adapter = Recording(GOOD)
    if missing == "answer off":
        value = config()
        value["roleSettings"]["roles"]["answer"]["enabled"] = False
        save(store, value)
    with pytest.raises(prompt_enhance.EnhanceError) as refused:
        run(store, {"one": adapter})
    assert (refused.value.code, refused.value.status) == ("not_configured", 503)
    assert adapter.calls == []


def set_privacy(store, chat, revision, **flags):
    privacy = {"memoryDisabled": False, "harnessDisabled": False, **flags}
    session_settings.save(
        store,
        chat,
        session_settings.Update(
            expected_revision=revision, settings={"agentId": "companion", "privacy": privacy}
        ),
    )


def test_a_chat_with_helpers_off_is_refused_and_a_private_chat_is_not(store):
    configured(store)
    adapter = Recording(GOOD)
    chat = store.create_session()
    set_privacy(store, chat, 0, memoryDisabled=True)
    assert run(store, {"one": adapter}, sessionId=chat)["modelId"] == "a"
    set_privacy(store, chat, 1, harnessDisabled=True)
    calls = len(adapter.calls)
    with pytest.raises(prompt_enhance.EnhanceError) as refused:
        run(store, {"one": adapter}, sessionId=chat)
    assert (refused.value.code, refused.value.status) == ("helpers_off", 409)
    assert len(adapter.calls) == calls


def test_an_unsaved_chat_can_only_add_privacy_and_an_unknown_chat_is_not_found(store):
    configured(store)
    adapter = Recording(GOOD)
    with pytest.raises(prompt_enhance.EnhanceError) as refused:
        run(store, {"one": adapter}, privacy={"harnessDisabled": True})
    assert refused.value.code == "helpers_off"
    assert run(store, {"one": adapter}, privacy={"harnessDisabled": False})["modelId"] == "a"
    with pytest.raises(prompt_enhance.EnhanceError) as unknown:
        run(store, {"one": adapter}, sessionId="ses_" + "0" * 16)
    assert unknown.value.status == 404


def test_the_owner_default_for_new_chats_applies_to_an_unsaved_chat(store):
    configured(store)
    revision = owner_settings.load(store)["revision"]
    owner_settings.save(
        store,
        owner_settings.Update(expected_revision=revision, values={"privacy.helpers": False}),
    )
    with pytest.raises(prompt_enhance.EnhanceError) as refused:
        run(store, {"one": Recording(GOOD)})
    assert refused.value.code == "helpers_off"


def test_a_template_id_is_refused_not_ignored(store):
    configured(store)
    adapter = Recording(GOOD)
    with pytest.raises(prompt_enhance.EnhanceError) as refused:
        run(store, {"one": adapter}, templateId="research")
    assert (refused.value.code, refused.value.status) == ("templates_unavailable", 422)
    assert adapter.calls == []


def paid_adapters(store, reply=GOOD, tokens=(100, 50)):
    paid = Paid(tokens=tokens)
    paid.complete_bounded = lambda messages, *, model, timeout: (
        paid.calls.append((model, timeout)) or Completion(reply, model, "openai", *tokens)
    )
    return paid, {"one": spending_cap.Capped(paid, store, lambda model: True)}


def test_a_paid_call_is_counted_and_the_cap_stops_the_next_one_before_it_is_sent(store):
    configured(store)
    set_cap(store, tokens=400)
    paid, adapters = paid_adapters(store)
    run(store, adapters)
    run(store, adapters)  # admitted: 150 used plus its input fits under 400
    with pytest.raises(prompt_enhance.EnhanceError) as stopped:
        run(store, adapters)
    assert (stopped.value.code, stopped.value.status) == ("spending_cap_reached", 503)
    assert stopped.value.message == spending_cap.REACHED
    assert len(paid.calls) == 2
    assert spending_cap.status(store)["used"]["calls"] == 2


def test_a_ledger_that_cannot_be_read_fails_closed(store, monkeypatch):
    configured(store)
    paid, adapters = paid_adapters(store)

    def broken(*args, **kwargs):
        raise RuntimeError("ledger offline")

    monkeypatch.setattr(spending_cap, "_admit", broken)
    with pytest.raises(prompt_enhance.EnhanceError) as refused:
        run(store, adapters)
    assert (refused.value.code, refused.value.status) == ("spending_cap_unavailable", 503)
    assert paid.calls == []


def test_the_ledger_row_holds_no_draft_text(store):
    configured(store)
    _, adapters = paid_adapters(store)
    run(store, adapters)
    with store._connect() as db:
        rows = [tuple(r) for r in db.execute("SELECT * FROM paid_model_calls")]
    assert len(rows) == 1 and "dogs" not in json.dumps(rows, default=str)


@pytest.mark.parametrize(
    "reply, text",
    [
        (GOOD, BETTER),
        ("```json\n" + GOOD + "\n```", BETTER),
        ("A plain rewritten prompt.", "A plain rewritten prompt."),
        (json.dumps({"text": "Hidden\U000e0041‮ removed"}), "Hidden removed"),
    ],
)
def test_usable_replies_are_accepted_and_cleaned(store, reply, text):
    configured(store)
    assert run(store, {"one": Recording(reply)})["text"] == text


@pytest.mark.parametrize(
    "reply",
    [
        "{not json",
        json.dumps({"text": ""}),
        json.dumps({"text": 5}),
        json.dumps(["list"]),
        json.dumps({"text": "x" * (prompt_enhance.REPLY_LIMIT + 1)}),
        "   ",
    ],
)
def test_unusable_replies_are_refused_after_one_call(store, reply):
    configured(store)
    adapter = Recording(reply)
    with pytest.raises(prompt_enhance.EnhanceError) as refused:
        run(store, {"one": adapter})
    assert (refused.value.code, refused.value.status) == ("unusable", 502)
    assert len(adapter.calls) == 1


def test_notes_are_cleaned_and_capped(store):
    configured(store)
    notes = ["one", "one", 5, "", "x" * 201, "two‮", "three", "four", "five", "six", "seven"]
    result = run(store, {"one": Recording(json.dumps({"text": "T", "notes": notes}))})
    assert result["notes"] == ["one", "two", "three", "four", "five"]


def test_a_reply_cut_at_the_length_limit_is_refused(store):
    configured(store)

    class Cut(Recording):
        def complete_bounded(self, messages, *, model, timeout):
            done = super().complete_bounded(messages, model=model, timeout=timeout)
            return Completion(done.text, model, "recording", raw={"finish_reason": "length"})

    with pytest.raises(prompt_enhance.EnhanceError) as refused:
        run(store, {"one": Cut(GOOD)})
    assert refused.value.code == "unusable"


def test_a_model_that_fails_is_unavailable_and_leaks_nothing(store):
    configured(store)

    class Down:
        name, timeout = "one", 5.0

        def complete_bounded(self, messages, *, model, timeout):
            raise ProviderUnavailable("PRIVATE_PROVIDER_TEXT " + messages[-1].content)

    with pytest.raises(prompt_enhance.EnhanceError) as refused:
        run(store, {"one": Down()})
    assert (refused.value.code, refused.value.status) == ("unavailable", 503)
    assert "PRIVATE" not in refused.value.message and "dogs" not in refused.value.message


def test_the_call_runs_inside_a_cancellable_inference(store):
    configured(store)
    seen = {}

    class Watching(Recording):
        def complete_bounded(self, messages, *, model, timeout):
            seen["token"] = inference_control.current()
            return super().complete_bounded(messages, model=model, timeout=timeout)

    run(store, {"one": Watching(GOOD)})
    assert seen["token"] is not None


@pytest.mark.parametrize(
    "fields",
    [
        {"text": ""},
        {"text": "   \n"},
        {"text": "x" * (prompt_enhance.TEXT_LIMIT + 1)},
        {"text": "ok", "sessionId": "not-a-session"},
        {"text": "ok", "extra": 1},
        {"text": "ok", "privacy": {"other": True}},
    ],
)
def test_the_request_is_strict_and_bounded(fields):
    with pytest.raises(ValidationError):
        prompt_enhance.Request(**fields)


# --- the route ---


@pytest.fixture
def http(store, monkeypatch):
    configured(store)
    adapter = Recording(GOOD)
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", ADMIN, raising=False)
    owner_hash = hashlib.sha256(OWNER.encode()).hexdigest()
    monkeypatch.setattr(api.app.state, "owner_key_hash", owner_hash, raising=False)
    router = type("Router", (), {"adapters": lambda self: {"one": adapter}})()
    monkeypatch.setattr(api.app.state, "router", router, raising=False)
    return TestClient(api.app), adapter


def test_the_owner_session_and_the_admin_key_may_enhance_and_others_may_not(http):
    client, adapter = http
    body = {"text": DRAFT}
    assert client.post("/prompts/enhance", json=body).status_code == 401
    wrong = client.post("/prompts/enhance", json=body, headers={"X-Pi-Owner-Key": "wrong"})
    assert wrong.status_code in {401, 403}
    for headers in ({"X-Pi-Owner-Key": OWNER}, {"X-Pi-Key": ADMIN}):
        reply = client.post("/prompts/enhance", json=body, headers=headers)
        assert reply.status_code == 200 and reply.json()["modelId"] == "a"
    assert len(adapter.calls) == 2


def test_a_bad_request_gets_the_static_422_without_echoing_the_draft(http):
    client, adapter = http
    secret = "SECRET-DRAFT-WORDS " * 600
    reply = client.post(
        "/prompts/enhance", json={"text": secret, "extra": 1}, headers={"X-Pi-Key": ADMIN}
    )
    assert reply.status_code == 422 and "SECRET" not in reply.text
    assert adapter.calls == []


def test_the_browser_contract_allows_the_session_alone():
    assert owner_allowed("POST", "/prompts/enhance")
    assert not owner_allowed("GET", "/prompts/enhance")
    assert not owner_allowed("POST", "/prompts/enhance/other")
    assert not runtime_allowed("POST", "/prompts/enhance")
    assert session_only_write("POST", "/prompts/enhance")


def test_the_openapi_names_the_reply_model(http):
    client, _ = http
    schema = client.get("/openapi.json").json()["paths"]["/prompts/enhance"]["post"]
    ok = schema["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok == {"$ref": "#/components/schemas/EnhancedPrompt"}
