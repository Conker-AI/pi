from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from gateway.api import Config, create_app
from gateway.diagnostics import collect_diagnostics
from gateway.store import AuthStore

ORIGIN = "https://conker.test"


def _config(tmp_path):
    return Config(
        ORIGIN,
        str(tmp_path / "auth.db"),
        "http://pi.test",
        "p" * 40,
        "http://toolgate.test",
        "o" * 40,
        pi_owner_key="c" * 40,
        toolgate_execution_key="tgx_" + "e" * 40,
    )


def _upstream(request: httpx.Request) -> httpx.Response:
    if request.url.host == "pi.test":
        return httpx.Response(
            200,
            json={
                "status": "degraded",
                "checks": {
                    "store": {"status": "ok"},
                    "memory": {"status": "not_configured"},
                    "local_provider": {"status": "unavailable"},
                    "hosted_provider": {"status": "not_configured"},
                    "action_boundary": {"status": "ok"},
                },
            },
        )
    return httpx.Response(200, json={"status": "ok", "results": []})


def test_diagnostic_contract_is_bounded_secret_free_and_actionable(tmp_path):
    config = _config(tmp_path)
    auth = AuthStore(config.database)
    with httpx.Client(transport=httpx.MockTransport(_upstream)) as upstream:
        report = collect_diagnostics(config, auth, upstream)

    assert report["schemaVersion"] == 1
    assert report["status"] == "attention"
    assert report["summary"] == {"attention": 3, "ok": 3, "optional": 2}
    assert [item["id"] for item in report["findings"]] == [
        "owner-login",
        "runtime",
        "owner-channel",
        "store",
        "memory",
        "local-model",
        "hosted-model",
        "actions",
    ]
    assert next(item for item in report["findings"] if item["id"] == "local-model")["recovery"] == {
        "label": "Check the local answer model",
        "uiRoute": "/settings",
        "command": "conker logs ollama",
    }
    rendered = str(report)
    assert config.pi_key not in rendered and config.owner_key not in rendered


def test_authenticated_gateway_endpoint_uses_the_same_contract(tmp_path):
    config = _config(tmp_path)
    auth = AuthStore(config.database)
    auth.set_password("correct horse battery staple", initial=True)
    with TestClient(
        create_app(config, store=auth, transport=httpx.MockTransport(_upstream)),
        base_url=ORIGIN,
    ) as client:
        anonymous = client.get("/api/diagnostics")
        assert anonymous.status_code == 401
        assert (
            client.post(
                "/auth/login",
                json={"password": "correct horse battery staple"},
                headers={
                    "Origin": ORIGIN,
                    "X-CSRF-Token": client.get("/auth/session").json()["csrf_token"],
                },
            ).status_code
            == 200
        )
        report = client.get("/api/diagnostics").json()
        assert client.get("/api/diagnostics?target=arbitrary").status_code == 422

    assert report["schemaVersion"] == 1
    assert report["findings"][0]["id"] == "owner-login"
    assert report["findings"][0]["status"] == "ok"
