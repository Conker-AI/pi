"""POST /inference: Conker's services call their own model roles through Pi (docs/inference.md)."""

import hashlib
import json
import time
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.api import server as api
from pi.conversation.store import Store
from pi.models import providers, service_inference
from pi.models import roles as model_roles
from pi.models.providers import Completion, ProviderUnavailable
from pi.turns import inference_control

MEMORYGATE, TOOLGATE, OWNER = "memorygate-key-synthetic", "toolgate-key-synthetic", "owner-key"
PROMPT = "Sort this private memory: the owner likes tea"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Adapter:
    def __init__(self, name, text='{"ok": true}', fail=False):
        self.name, self.text, self.fail = name, text, fail
        self.calls = []

    def complete_bounded(self, messages, *, model, timeout):
        token = inference_control.current()
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "timeout": timeout,
                "output": providers._output.get(),
                "background": inference_control._background.get(),
                "token": token,
            }
        )
        if self.fail:
            raise ProviderUnavailable("offline")
        return Completion(self.text, model + "-actual", self.name, 12, 3, cost_usd=None)


class Router:
    def __init__(self, local=None, local_model="qwen3:4b", others=()):
        self.local, self.local_model = local, local_model
        self.providers = {adapter.name: adapter for adapter in others}

    def adapters(self):
        return {**self.providers, **({self.local.name: self.local} if self.local else {})}


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


@pytest.fixture(scope="module")
def unwritten(tmp_path_factory):
    """One store for the refusals, which never write: a store takes seconds to set up."""
    with closing(Store(tmp_path_factory.mktemp("refusals") / "pi.db")) as value:
        yield value
        assert not calls(value)


def client(monkeypatch, store, router, keys=None):
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "router", router, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin-key", raising=False)
    monkeypatch.setattr(api.app.state, "owner_key_hash", digest(OWNER), raising=False)
    monkeypatch.setattr(
        api.app.state,
        "service_key_hashes",
        {"memorygate": digest(MEMORYGATE), "toolgate": digest(TOOLGATE)} if keys is None else keys,
        raising=False,
    )
    return TestClient(api.app)


def body(**changes):
    return {
        "role": "memory-sorting",
        "system": "Return JSON.",
        "prompt": PROMPT,
        "max_tokens": 160,
        "json": True,
        "request_id": "memorygate:sort:0001",
        **changes,
    }


def save_role(store, role, *, model="hosted-model", fallback=None, timeout=120000):
    configuration = {
        "providers": [{"id": "hosted", "name": "Hosted", "enabled": True}],
        "models": [
            {
                "id": identity,
                "providerId": "hosted",
                "name": identity,
                "route": "route-" + identity,
                "enabled": True,
                "routingDescription": "",
            }
            for identity in ("hosted-model", "backup")
        ],
        "defaultModelId": "hosted-model",
        "roleSettings": {
            "answerMode": "manual",
            "roles": {
                name: {
                    "enabled": False,
                    "eligibleModelIds": [],
                    "modelId": None,
                    "timeoutMs": 120000,
                    "failure": "stop",
                    "fallbackModelId": None,
                }
                for name in model_roles.ROLES
            },
        },
    }
    if model is not None:
        configuration["roleSettings"]["roles"][role] = {
            "enabled": True,
            "eligibleModelIds": [model, *([fallback] if fallback else [])],
            "modelId": model,
            "timeoutMs": timeout,
            "failure": "fallback" if fallback else "stop",
            "fallbackModelId": fallback,
        }
    saved = model_roles.load(store)
    model_roles.save(
        store,
        model_roles.Update(
            expected_revision=saved["revision"],
            configuration=model_roles.Configuration.model_validate(configuration),
        ),
    )


def calls(store):
    with store._connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM service_model_calls")]


def test_without_a_chosen_model_the_role_uses_the_local_model(monkeypatch, store):
    local = Adapter("ollama")
    response = client(monkeypatch, store, Router(local)).post(
        "/inference", json=body(), headers={"X-Pi-Service-Key": MEMORYGATE}
    )
    assert response.status_code == 200
    assert response.json() == {
        "text": '{"ok": true}',
        "model": "qwen3:4b-actual",
        "provider": "ollama",
    }
    (call,) = local.calls
    assert call["model"] == "qwen3:4b" and call["timeout"] == 300.0
    assert [(m.role, m.content) for m in call["messages"]] == [
        ("system", "Return JSON."),
        ("user", PROMPT),
    ]
    assert call["output"] == providers.Output(json=True, max_tokens=160, think=False)
    # Memory sorting yields the local model to the owner's replies.
    assert call["background"] is True and call["token"] is not None


def test_toolgate_uses_its_own_role_in_the_foreground_and_may_skip_the_system_text(
    monkeypatch, store
):
    local = Adapter("ollama", text="Plain text")
    response = client(monkeypatch, store, Router(local)).post(
        "/inference",
        json=body(role="tool-text", system="", json=False),
        headers={"X-Pi-Service-Key": TOOLGATE},
    )
    assert response.status_code == 200 and response.json()["text"] == "Plain text"
    (call,) = local.calls
    assert [m.role for m in call["messages"]] == ["user"]
    assert call["output"] == providers.Output(json=False, max_tokens=160, think=False)
    assert call["background"] is False


def test_the_owners_choice_is_used_with_its_fallback(monkeypatch, store):
    local, hosted = Adapter("ollama"), Adapter("hosted", fail=True)
    save_role(store, "memory-sorting", fallback="backup", timeout=600000)
    response = client(monkeypatch, store, Router(local, others=[hosted])).post(
        "/inference", json=body(), headers={"X-Pi-Service-Key": MEMORYGATE}
    )
    # Both models share the failing adapter: the fallback is tried, then the call is refused.
    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "unavailable", "message": "The model did not answer. Try again later."}
    }
    assert [call["model"] for call in hosted.calls] == ["route-hosted-model", "route-backup"]
    assert all(call["timeout"] == 600.0 for call in hosted.calls) and not local.calls
    hosted.fail = False
    response = client(monkeypatch, store, Router(local, others=[hosted])).post(
        "/inference", json=body(), headers={"X-Pi-Service-Key": MEMORYGATE}
    )
    assert response.json()["provider"] == "hosted"
    assert response.json()["model"] == "route-hosted-model-actual"


def test_a_role_switched_off_goes_back_to_the_local_model(monkeypatch, store):
    local, hosted = Adapter("ollama"), Adapter("hosted")
    save_role(store, "memory-sorting", model=None)
    response = client(monkeypatch, store, Router(local, others=[hosted])).post(
        "/inference", json=body(), headers={"X-Pi-Service-Key": MEMORYGATE}
    )
    assert response.json()["provider"] == "ollama" and not hosted.calls
    # The saved wait is kept; only a role that was never saved gets the local default.
    assert local.calls[0]["timeout"] == 120.0


def test_no_usable_model_is_a_503_no_model(monkeypatch, store):
    response = client(monkeypatch, store, Router(None)).post(
        "/inference", json=body(), headers={"X-Pi-Service-Key": MEMORYGATE}
    )
    assert response.status_code == 503 and response.json()["detail"]["code"] == "no_model"
    # The chosen model's provider is not set up on this server.
    save_role(store, "memory-sorting")
    response = client(monkeypatch, store, Router(Adapter("ollama"))).post(
        "/inference", json=body(), headers={"X-Pi-Service-Key": MEMORYGATE}
    )
    assert response.status_code == 503 and response.json()["detail"]["code"] == "no_model"
    assert calls(store) == []


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Pi-Service-Key": "wrong"}, {"X-Pi-Service-Key": ""}, {"X-Pi-Owner-Key": OWNER}],
)
def test_only_a_service_key_may_call(monkeypatch, unwritten, headers):
    local = Adapter("ollama")
    response = client(monkeypatch, unwritten, Router(local)).post(
        "/inference", json=body(), headers=headers
    )
    assert response.status_code == 401 and not local.calls


@pytest.mark.parametrize("keys", [{}, None])
def test_no_configured_keys_refuse_everyone(monkeypatch, unwritten, keys):
    local = Adapter("ollama")
    test_client = client(monkeypatch, unwritten, Router(local), keys={})
    if keys is None:
        monkeypatch.delattr(api.app.state, "service_key_hashes")
    response = test_client.post("/inference", json=body(), headers={"X-Pi-Service-Key": MEMORYGATE})
    assert response.status_code == 401 and not local.calls


@pytest.mark.parametrize(
    "key,role",
    [(MEMORYGATE, "tool-text"), (TOOLGATE, "memory-sorting"), (MEMORYGATE, "answer")],
)
def test_each_service_may_use_only_its_own_role(monkeypatch, unwritten, key, role):
    local = Adapter("ollama")
    response = client(monkeypatch, unwritten, Router(local)).post(
        "/inference", json=body(role=role), headers={"X-Pi-Service-Key": key}
    )
    assert response.status_code == 403 and not local.calls
    assert response.json()["detail"]["code"] == "role_not_allowed"


@pytest.mark.parametrize(
    "changes",
    [
        {"max_tokens": 7},
        {"max_tokens": 4097},
        {"max_tokens": "160"},
        {"json": "true"},
        {"system": "x" * 16001},
        {"prompt": "x" * 64001},
        {"prompt": ""},
        {"request_id": "short"},
        {"request_id": "x" * 129},
        {"request_id": "has spaces in it, too"},
        {"role": ""},
        {"temperature": 0.2},
        {"as_json": True},
    ],
)
def test_the_body_is_strict(monkeypatch, unwritten, changes):
    local = Adapter("ollama")
    test_client = client(monkeypatch, unwritten, Router(local))
    response = test_client.post(
        "/inference", json=body(**changes), headers={"X-Pi-Service-Key": MEMORYGATE}
    )
    assert response.status_code == 422 and not local.calls
    # A bad key is refused before the body is looked at.
    response = test_client.post(
        "/inference", json=body(**changes), headers={"X-Pi-Service-Key": "wrong"}
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "key,role,request_id",
    [
        (MEMORYGATE, "memory-sorting", "memorygate." + "0123456789abcdef" * 2),
        (TOOLGATE, "tool-text", "toolgate:" + "fedcba9876543210" * 2),
    ],
)
def test_the_services_request_ids_are_accepted(monkeypatch, store, key, role, request_id):
    local = Adapter("ollama")
    response = client(monkeypatch, store, Router(local)).post(
        "/inference",
        json=body(role=role, request_id=request_id),
        headers={"X-Pi-Service-Key": key},
    )
    assert response.status_code == 200
    assert [row["request_id"] for row in calls(store)] == [request_id]


def test_the_largest_body_is_accepted(monkeypatch, store):
    local = Adapter("ollama")
    response = client(monkeypatch, store, Router(local)).post(
        "/inference",
        json=body(system="s" * 16000, prompt="p" * 64000, max_tokens=4096, request_id="r" * 128),
        headers={"X-Pi-Service-Key": MEMORYGATE},
    )
    assert response.status_code == 200 and len(local.calls) == 1


def test_an_answer_past_max_tokens_is_refused(monkeypatch, store):
    local = Adapter("ollama", text="x" * (8 * 16 + 1))
    response = client(monkeypatch, store, Router(local)).post(
        "/inference", json=body(max_tokens=8), headers={"X-Pi-Service-Key": MEMORYGATE}
    )
    assert response.status_code == 503 and response.json()["detail"]["code"] == "unavailable"


def test_usage_is_recorded_without_any_text(monkeypatch, store):
    local = Adapter("ollama")
    test_client = client(monkeypatch, store, Router(local))
    test_client.post("/inference", json=body(), headers={"X-Pi-Service-Key": MEMORYGATE})
    local.fail = True
    test_client.post(
        "/inference",
        json=body(role="tool-text", request_id="toolgate:run:000002"),
        headers={"X-Pi-Service-Key": TOOLGATE},
    )
    rows = sorted(calls(store), key=lambda row: row["started_at"])
    assert [(r["service"], r["role"], r["status"]) for r in rows] == [
        ("memorygate", "memory-sorting", "completed"),
        ("toolgate", "tool-text", "failed"),
    ]
    first, second = rows
    assert first["request_id"] == "memorygate:sort:0001"
    assert (first["provider"], first["model"], first["model_id"]) == (
        "ollama",
        "qwen3:4b",
        service_inference.LOCAL_MODEL_ID,
    )
    assert first["actual_model"] == "qwen3:4b-actual"
    assert (first["input_tokens"], first["output_tokens"], first["cost_usd"]) == (12, 3, None)
    assert second["failure_kind"] == "provider_error" and second["elapsed_ms"] >= 0
    with store._connect() as db:
        dump = "\n".join(db.iterdump())
    assert PROMPT not in dump and "Return JSON." not in dump and '{"ok": true}' not in dump

    response = test_client.get("/inference/usage", headers={"X-Pi-Owner-Key": OWNER})
    assert response.status_code == 200
    usage = response.json()
    assert len(usage["calls"]) == 2
    assert usage["lastDay"] == [
        {
            "service": "memorygate",
            "role": "memory-sorting",
            "calls": 1,
            "failed": 0,
            "input_tokens": 12,
            "output_tokens": 3,
            "cost_usd": None,
        },
        {
            "service": "toolgate",
            "role": "tool-text",
            "calls": 1,
            "failed": 1,
            "input_tokens": None,
            "output_tokens": None,
            "cost_usd": None,
        },
    ]
    assert test_client.get("/inference/usage").status_code == 401
    assert (
        test_client.get("/inference/usage", headers={"X-Pi-Service-Key": MEMORYGATE}).status_code
        == 401
    )


def test_old_usage_is_dropped_after_thirty_days(monkeypatch, store):
    with store._connect() as db:
        db.execute(
            "INSERT INTO service_model_calls(id,service,role,request_id,provider,model,status,"
            "started_at) VALUES('old','memorygate','memory-sorting','memorygate:sort:old0',"
            "'ollama','qwen3:4b','completed',?)",
            (time.time() - 31 * 86400,),
        )
        db.commit()
    client(monkeypatch, store, Router(Adapter("ollama"))).post(
        "/inference", json=body(), headers={"X-Pi-Service-Key": MEMORYGATE}
    )
    assert [row["id"] for row in calls(store) if row["id"] == "old"] == []


def test_a_call_past_the_roles_wait_is_stopped(monkeypatch, store):
    class Slow(Adapter):
        def complete_bounded(self, messages, *, model, timeout):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                inference_control.check()
                time.sleep(0.01)
            return Completion("too late", model, self.name)

    save_role(store, "memory-sorting", timeout=100)
    hosted = Slow("hosted")
    started = time.monotonic()
    response = client(monkeypatch, store, Router(None, others=[hosted])).post(
        "/inference", json=body(), headers={"X-Pi-Service-Key": MEMORYGATE}
    )
    assert response.status_code == 503 and response.json()["detail"]["code"] == "unavailable"
    assert time.monotonic() - started < 4
    assert calls(store)[0]["failure_kind"] == "timeout"


def test_a_service_has_a_bounded_number_of_calls_at_once(monkeypatch, unwritten):
    local = Adapter("ollama")
    slots = service_inference._slots["memorygate"]
    for _ in range(service_inference.IN_FLIGHT):
        assert slots.acquire(blocking=False)
    try:
        response = client(monkeypatch, unwritten, Router(local)).post(
            "/inference", json=body(), headers={"X-Pi-Service-Key": MEMORYGATE}
        )
        assert response.status_code == 503 and response.json()["detail"]["code"] == "unavailable"
        assert not local.calls
    finally:
        for _ in range(service_inference.IN_FLIGHT):
            slots.release()


def test_service_keys_come_from_a_json_object_of_hashes():
    one, two = digest("one"), digest("two")
    assert service_inference.key_hashes("") == {}
    assert service_inference.key_hashes("  {}  ") == {}
    assert service_inference.key_hashes(
        json.dumps({"memorygate": one.upper(), "toolgate": two})
    ) == {"memorygate": one, "toolgate": two}
    for raw in (
        "not json",
        "[]",
        json.dumps({"systemgate": one}),
        json.dumps({"memorygate": "abc"}),
        json.dumps({"memorygate": 5}),
        json.dumps({"memorygate": one, "toolgate": one}),
    ):
        with pytest.raises(ValueError):
            service_inference.key_hashes(raw)


def test_a_key_names_its_service_only():
    hashes = {"memorygate": digest(MEMORYGATE), "toolgate": digest(TOOLGATE)}
    assert service_inference.authenticate(hashes, MEMORYGATE) == "memorygate"
    assert service_inference.authenticate(hashes, TOOLGATE) == "toolgate"
    assert service_inference.authenticate(hashes, digest(MEMORYGATE)) is None
    assert service_inference.authenticate(hashes, None) is None
    assert service_inference.authenticate({"memorygate": digest("")}, "") is None


@pytest.mark.parametrize("stream", [False, True])
def test_ollama_applies_the_requested_output_and_nothing_otherwise(monkeypatch, stream):
    import httpx

    sent = []
    final = {"model": "qwen3:4b", "message": {"content": "{}"}, "done": True}

    def post(url, **kwargs):
        sent.append(kwargs["json"])
        return httpx.Response(200, json=final, request=httpx.Request("POST", url))

    class Streamed:
        def __init__(self, payload):
            sent.append(payload)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def raise_for_status(self):
            pass

        def iter_lines(self):
            yield json.dumps(final)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "stream", lambda method, url, **kwargs: Streamed(kwargs["json"]))
    monkeypatch.setattr(
        httpx.Client, "stream", lambda self, method, url, **kwargs: Streamed(kwargs["json"])
    )
    adapter = providers.OllamaProvider("http://127.0.0.1:1")

    def complete():
        if not stream:
            return adapter.complete_bounded([], model="qwen3:4b", timeout=5)
        with inference_control.bind(inference_control.Token()):
            return adapter.complete_bounded([], model="qwen3:4b", timeout=5)

    complete()
    with providers.output(providers.Output(json=True, max_tokens=64, think=False)):
        complete()
    with providers.output(providers.Output(max_tokens=32)):
        complete()
    plain, wanted, limited = sent
    assert {"format", "options", "think"}.isdisjoint(plain)
    assert wanted["format"] == "json" and wanted["options"] == {"num_predict": 64}
    assert wanted["think"] is False
    assert "format" not in limited and "think" not in limited
    assert limited["options"] == {"num_predict": 32}
    assert providers._output.get() is None


def start(monkeypatch, tmp_path, keys):
    import asyncio
    import os

    import httpx

    from pi.api import startup

    for name in list(os.environ):
        if name.startswith("PI_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("PI_ADMIN_KEY", "synthetic-admin-key-only")
    monkeypatch.setenv("PI_OWNER_KEY_SHA256", digest(OWNER))
    monkeypatch.setenv("PI_DB_PATH", str(tmp_path / "startup.db"))
    monkeypatch.setenv("PI_SERVICE_KEYS_SHA256", keys)

    def no_network(*args, **kwargs):
        pytest.fail("Startup must not contact external services")

    monkeypatch.setattr(httpx, "post", no_network)
    monkeypatch.setattr(httpx, "get", no_network)
    seen = {}

    async def run():
        async with startup.lifespan(api.app):
            seen.update(api.app.state.service_key_hashes)

    asyncio.run(run())
    return seen


def test_startup_reads_the_service_keys(monkeypatch, tmp_path):
    keys = {"memorygate": digest(MEMORYGATE), "toolgate": digest(TOOLGATE)}
    assert start(monkeypatch, tmp_path, json.dumps(keys)) == keys


@pytest.mark.parametrize(
    "keys",
    [
        "not json",
        json.dumps({"systemgate": digest(MEMORYGATE)}),
        json.dumps({"memorygate": digest(OWNER)}),
        json.dumps({"toolgate": digest("synthetic-admin-key-only")}),
    ],
)
def test_startup_refuses_bad_or_reused_service_keys(monkeypatch, tmp_path, keys):
    with pytest.raises(RuntimeError):
        start(monkeypatch, tmp_path, keys)
    # Refused before anything was opened.
    assert not (tmp_path / "startup.db").exists()
