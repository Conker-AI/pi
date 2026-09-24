import json

import httpx
import pytest
from test_model_roles import Adapter, config

from pi import model_roles
from pi.decision_provider import DecisionProvider, configured
from pi.providers import Message, ProviderUnavailable


def setup(transport):
    value = config()
    value["providers"].append({"id": "decisions", "name": "Local decisions", "enabled": True})
    value["models"].append(
        {
            "id": "laya",
            "providerId": "decisions",
            "name": "Local Laya",
            "route": "model-routing",
            "enabled": True,
        }
    )
    value["roleSettings"]["answerMode"] = "router"
    value["roleSettings"]["roles"]["routing"] = {
        "enabled": True,
        "eligibleModelIds": ["laya", "a"],
        "modelId": "laya",
        "timeoutMs": 1000,
        "failure": "fallback",
        "fallbackModelId": "a",
    }
    adapter = DecisionProvider("http://127.0.0.1:8060", "x" * 32, transport=transport)
    return value, adapter


def test_real_dispatch_uses_typed_service_and_retains_manual_privacy():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        assert request.headers["x-decision-key"] == "x" * 32
        return httpx.Response(
            200,
            json={
                "choice": "b",
                "confidence": 0.8,
                "model": "laya-pinned",
                "probabilities": {"a": 0.2, "b": 0.8},
            },
        )

    value, decision = setup(httpx.MockTransport(handler))
    value["models"][0]["routingDescription"] = "Fast simple answers"
    providers = {"decisions": decision, "one": Adapter(), "two": Adapter()}
    result = model_roles.dispatch(value, "answer", [Message("user", "Explain this")], providers)
    assert result["modelId"] == "b"
    assert result["attempts"][0]["actualModel"] == "laya-pinned"
    assert calls[0]["choices"] == {"a": "Fast simple answers", "b": "B"}
    with pytest.raises(ProviderUnavailable):
        model_roles.dispatch(
            value, "answer", [Message("user", "Private")], providers, harness_disabled=True
        )
    result = model_roles.dispatch(
        value, "answer", [], providers, override="a", harness_disabled=True
    )
    assert result["modelId"] == "a" and len(calls) == 1


def test_service_failure_uses_only_explicit_helper_fallback():
    value, decision = setup(httpx.MockTransport(lambda request: httpx.Response(503)))
    result = model_roles.dispatch(
        value,
        "answer",
        [Message("user", "Hi")],
        {"decisions": decision, "one": Adapter('{"modelId":"b"}'), "two": Adapter()},
    )
    assert result["modelId"] == "b"
    assert result["attempts"][0]["status"] == "unavailable"
    assert result["attempts"][1]["modelId"] == "a"


def test_routing_projection_excludes_system_memory_and_old_history_without_truncating_request():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choice": "b",
                "confidence": 0.8,
                "model": "laya-pinned",
                "probabilities": {"a": 0.2, "b": 0.8},
            },
        )

    _, decision = setup(httpx.MockTransport(handler))
    envelope = {
        "allowedModelIds": ["a", "b"],
        "modelDescriptions": {"a": "Simple", "b": "Complex"},
        "task": [
            {"role": "system", "content": "private system and retrieved memory" * 100},
            {"role": "user", "content": "old private question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "Current task"},
        ],
    }

    def invoke():
        return decision.complete_bounded(
            [Message("system", "route"), Message("user", json.dumps(envelope))],
            model="model-routing",
            timeout=1,
        )

    result = invoke()
    assert json.loads(calls[0]["state"]) == [{"role": "user", "content": "Current task"}]
    assert result.raw["decision"]["inputScope"] == "latest-user-request"
    envelope["task"][-1]["content"] = "x" * 1601
    with pytest.raises(ProviderUnavailable):
        invoke()
    assert len(calls) == 1


def test_adapter_refuses_text_answering_and_unapproved_choices():
    _, decision = setup(
        httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"choice": "unapproved", "confidence": 1, "model": "laya"}
            )
        )
    )
    with pytest.raises(ProviderUnavailable):
        decision.complete_bounded([Message("user", "hello")], model="answer", timeout=1)
    with pytest.raises(ProviderUnavailable):
        decision.complete_bounded(
            [
                Message("system", "route"),
                Message(
                    "user",
                    json.dumps(
                        {
                            "allowedModelIds": ["a", "b"],
                            "modelDescriptions": {"a": "Simple", "b": "Complex"},
                            "task": [],
                        }
                    ),
                ),
            ],
            model="model-routing",
            timeout=1,
        )
    assert configured({}) == {}
    for url in ["http://evil.example", "http://localhost/?key=foo", "http://user:pass@localhost"]:
        with pytest.raises(ValueError):
            DecisionProvider(url, "x" * 32)


def test_memory_ranking_preserves_records_and_falls_back_without_dropping_evidence():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choice": "m1",
                "confidence": 0.8,
                "model": "local",
                "probabilities": {"m0": 0.2, "m1": 0.8},
            },
        )

    _, decision = setup(httpx.MockTransport(handler))
    package = {
        "memories": [
            {"id": "one", "text": "Original first", "confidence": "low"},
            {"id": "two", "text": "Original second", "source_id": "proof"},
        ],
        "scope": "selected",
        "retrieval": {"semantic": {"status": "ok"}},
    }
    result = decision.rank_memories("query", package)
    assert result["memories"] == list(reversed(package["memories"]))
    assert result["scope"] == "selected" and result["retrieval"]["semantic"] == {"status": "ok"}
    assert result["retrieval"]["reranking"]["status"] == "ranked"
    assert calls[0]["choices"] == {"m0": "Memory m0", "m1": "Memory m1"}
    decision.transport = httpx.MockTransport(lambda request: httpx.Response(503))
    result = decision.rank_memories("query", package)
    assert result["memories"] == package["memories"]
    assert result["retrieval"]["reranking"]["status"] == "fallback"


def test_memory_no_harness_never_calls_ranker(monkeypatch):
    from types import SimpleNamespace

    from pi import memory, memory_store, session_settings

    selected = {
        "kind": "companion",
        "agentId": "companion",
        "revision": 0,
        "privacy": {"harnessDisabled": True},
    }
    monkeypatch.setattr(session_settings, "execution", lambda *args: selected)
    monkeypatch.setattr(session_settings, "memory_allowed", lambda value: True)
    monkeypatch.setattr(memory_store, "pending_deletions", lambda store: False)
    saved = []
    monkeypatch.setattr(memory_store, "save_context", lambda *args: saved.append(args))
    package = {"memories": [], "retrieval": {"semantic": {"status": "ok"}}}
    client = SimpleNamespace(retrieve=lambda query: package)

    def prohibited(*args):
        pytest.fail("No-harness context reached the decision service")

    worker = memory.Memory(
        SimpleNamespace(get_turn=lambda identity: {"session_id": "session"}),
        client,
        ranker=SimpleNamespace(rank_memories=prohibited),
    )
    worker.prepare("turn", "private")
    assert saved[-1][-1] is package


def test_typed_memory_ranking_route():
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choice": "m1",
                "confidence": 0.8,
                "model": "replacement-model",
                "probabilities": {"m0": 0.1, "m1": 0.9},
            },
        )

    _, provider = setup(httpx.MockTransport(handler))
    result = provider.complete_bounded(
        [
            Message(
                "user",
                json.dumps(
                    {"query": "question", "memory_previews": {"m0": "first", "m1": "second"}}
                ),
            )
        ],
        model="memory-ranking",
        timeout=1,
    )
    assert json.loads(result.text) == {"order": ["m1", "m0"]}
    assert result.model == "replacement-model"
    assert len(calls) == 1
