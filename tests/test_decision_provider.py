import json

import httpx
import pytest

from pi import model_roles
from pi.decision_provider import DecisionProvider, configured
from pi.providers import Message, ProviderUnavailable
from test_model_roles import Adapter, config


def setup(transport):
    value = config()
    value["providers"].append({"id": "decisions", "name": "Local decisions", "enabled": True})
    value["models"].append({"id": "laya", "providerId": "decisions", "name": "Local Laya",
                             "route": "model-routing", "enabled": True})
    value["roleSettings"]["answerMode"] = "router"
    value["roleSettings"]["roles"]["routing"] = {
        "enabled": True, "eligibleModelIds": ["laya", "a"], "modelId": "laya",
        "timeoutMs": 1000, "failure": "fallback", "fallbackModelId": "a"}
    adapter = DecisionProvider("http://127.0.0.1:8060", "x" * 32, transport=transport)
    return value, adapter


def test_real_dispatch_uses_typed_service_and_retains_manual_privacy():
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        assert request.headers["x-decision-key"] == "x" * 32
        return httpx.Response(200, json={"choice": "b", "confidence": 0.8, "model": "laya-pinned"})
    value, decision = setup(httpx.MockTransport(handler))
    providers = {"decisions": decision, "one": Adapter(), "two": Adapter()}
    result = model_roles.dispatch(value, "answer", [Message("user", "Explain this")], providers)
    assert result["modelId"] == "b"
    assert result["attempts"][0]["actualModel"] == "laya-pinned"
    assert calls[0]["choices"] == {"a": "A", "b": "B"}
    with pytest.raises(ProviderUnavailable):
        model_roles.dispatch(value, "answer", [Message("user", "Private")], providers, harness_disabled=True)
    result = model_roles.dispatch(value, "answer", [], providers, override="a", harness_disabled=True)
    assert result["modelId"] == "a" and len(calls) == 1


def test_service_failure_uses_only_explicit_helper_fallback():
    value, decision = setup(httpx.MockTransport(lambda request: httpx.Response(503)))
    result = model_roles.dispatch(value, "answer", [Message("user", "Hi")],
        {"decisions": decision, "one": Adapter('{"modelId":"b"}'), "two": Adapter()})
    assert result["modelId"] == "b"
    assert result["attempts"][0]["status"] == "unavailable"
    assert result["attempts"][1]["modelId"] == "a"


def test_adapter_refuses_text_answering_and_unapproved_choices():
    _, decision = setup(httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choice": "unapproved", "confidence": 1, "model": "laya"})))
    with pytest.raises(ProviderUnavailable):
        decision.complete_bounded([Message("user", "hello")], model="answer", timeout=1)
    with pytest.raises(ProviderUnavailable):
        decision.complete_bounded([Message("system", "route"), Message("user", json.dumps({
            "allowedModelIds": ["a", "b"], "modelDescriptions": {"a": "Simple", "b": "Complex"},
            "task": []}))], model="model-routing", timeout=1)
    assert configured({}) == {}
    for url in ["http://evil.example", "http://localhost/?key=foo", "http://user:pass@localhost"]:
        with pytest.raises(ValueError):
            DecisionProvider(url, "x" * 32)
