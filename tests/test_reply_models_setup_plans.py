"""The plan usage, setup boundaries and setup model routes describe their replies in OpenAPI
(D-01), and what they send matches the description."""

import hashlib
import json
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient

from pi.actions import toolgate as toolgate_module
from pi.api import server as api
from pi.conversation.store import Store
from pi.models.roles import ConfigurationDocument
from pi.owner import plan_alerts
from pi.owner.plan_replies import PlanUsageOverview
from pi.setup.boundary_replies import ToolGatePolicySummary
from tests.test_plan_alerts import LATER, NOW, plan
from tests.test_setup_models import Router

ADMIN = "reply-models-admin-key"
ROUTES = {
    ("get", "/plans/usage"): PlanUsageOverview,
    ("get", "/setup/boundaries"): ToolGatePolicySummary,
    ("post", "/setup/models"): ConfigurationDocument,
}


def policy_summary():
    """The summary as ToolGate's GET /v2/agent/policy-summary builds it (digest included)."""
    evidence = {
        "lockdown": False,
        "scopePatterns": ["*"],
        "tools": [
            {
                "id": "web.search",
                "name": "Web search",
                "authorization": "auto",
                "executionType": "builtin",
                "usageLimits": {"max_per_minute": 6, "cooldown_seconds": 0},
                "definitionDigest": "d" * 64,
            }
        ],
    }
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return {**evidence, "digest": hashlib.sha256(canonical.encode()).hexdigest()}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", ADMIN, raising=False)
        monkeypatch.setattr(api.app.state, "router", Router(), raising=False)
        monkeypatch.setattr(
            api.app.state,
            "toolgate",
            toolgate_module.ToolGateClient("http://toolgate.test", "execution-key"),
            raising=False,
        )
        monkeypatch.setattr(
            toolgate_module.httpx,
            "get",
            lambda url, **_: httpx.Response(
                200, json=policy_summary(), request=httpx.Request("GET", url)
            ),
        )
        yield TestClient(api.app, headers={"X-Pi-Key": ADMIN}), store


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_each_route_names_its_reply_model_in_openapi(method, path):
    responses = api.app.openapi()["paths"][path][method]["responses"]
    schema = responses["200"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": f"#/components/schemas/{ROUTES[method, path].__name__}"}


def test_plan_usage_matches_its_model_with_and_without_readings(client):
    http, store = client
    empty = http.get("/plans/usage")
    assert empty.json() == {"windows": [], "alerts": []}
    PlanUsageOverview.model_validate_json(empty.text)
    plan_alerts.record(store, plan(90, resets=LATER), 80, now=NOW)
    plan_alerts.record(store, plan(10, provider="claude", resets=None), 80, now=NOW)
    reply = http.get("/plans/usage")
    parsed = PlanUsageOverview.model_validate_json(reply.text)
    assert [window.provider for window in parsed.windows] == ["chatgpt", "claude"]
    assert parsed.windows[1].resetsAt is None
    assert [alert.usedPercent for alert in parsed.alerts] == [90]
    assert parsed.model_dump(mode="json") == reply.json()


def test_setup_boundaries_passes_toolgates_summary_through_as_described(client):
    http, _ = client
    reply = http.get("/setup/boundaries")
    assert reply.json() == policy_summary()
    ToolGatePolicySummary.model_validate_json(reply.text)


def test_setup_model_selection_matches_its_model(client):
    http, _ = client
    reply = http.post("/setup/models", json={"candidateId": "local-answer", "expectedRevision": 0})
    assert reply.status_code == 200
    parsed = ConfigurationDocument.model_validate_json(reply.text)
    assert parsed.revision == 1 and parsed.configuration is not None
    assert parsed.configuration.defaultModelId == "local-answer"
    assert reply.json() == http.get("/models/configuration").json()
