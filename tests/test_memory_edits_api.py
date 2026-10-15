"""Owner/Gateway boundaries for text corrections; upstream memory is synthetic."""

import hashlib
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_memory_edits import BODY, setup  # noqa: F401 -- shared isolated fixture

from gateway import mutation
from gateway.api import Config, create_app
from gateway.store import AuthStore
from pi.api import browser_contract
from pi.api import server as api
from pi.memory import edits

OWNER = "synthetic-owner-key-" + "o" * 32
PREFIX = "/memory/corrections"


@pytest.fixture
def http(setup, monkeypatch):
    store, client, _ = setup
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin", raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    monkeypatch.setattr(api.app.state, "memory_corrections", client, raising=False)
    monkeypatch.setattr(api.app.state, "memory", SimpleNamespace(client=client), raising=False)
    return TestClient(api.app)


def test_actual_owner_snapshot_apply_and_read_recovery(http, setup):
    store, _, remote = setup
    headers = {"X-Pi-Owner-Key": OWNER}
    assert http.get(PREFIX + "/memories/mem-1").status_code == 401
    assert (
        http.get(PREFIX + "/memories/mem-1", headers={"X-Pi-Gateway-Key": "runtime"}).status_code
        == 401
    )
    snapshot = http.get(PREFIX + "/memories/mem-1", headers=headers)
    assert snapshot.status_code == 200 and snapshot.json()["sourceType"] == "inferred"
    remote.lose = True
    result = http.post(PREFIX, json=BODY.model_dump(), headers=headers)
    assert result.status_code == 200 and result.json()["status"] == "unknown"
    assert (
        http.get(PREFIX + "/pending", headers=headers).json()["pending"]["requestId"]
        == BODY.request_id
    )
    before = len(remote.calls)
    response = http.get(PREFIX + "/" + BODY.request_id, headers=headers)
    assert response.json()["status"] == "applied"
    assert remote.calls[before:] == [("GET", "/runtime/corrections/" + BODY.request_id)]
    assert edits.pending(store)["pending"] is None


def test_status_local404_is_distinct_from_remote_missing_receipt(http, setup):
    _, _, remote = setup
    headers = {"X-Pi-Owner-Key": OWNER}
    assert http.get(PREFIX + "/unknown_request_0001", headers=headers).status_code == 404
    remote.before_dispatch = True
    http.post(PREFIX, json=BODY.model_dump(), headers=headers)
    result = http.get(PREFIX + "/" + BODY.request_id, headers=headers)
    assert result.status_code == 200 and result.json()["status"] == "unknown"


def test_exact_retry_after_dispatch_failure(http, setup):
    _, _, remote = setup
    headers = {"X-Pi-Owner-Key": OWNER}
    remote.before_dispatch = True
    value = http.post(PREFIX, json=BODY.model_dump(), headers=headers).json()
    remote.before_dispatch = False
    result = http.post(
        PREFIX + "/" + BODY.request_id + "/retry",
        json={"expected_payload_hash": value["payloadHash"]},
        headers=headers,
    )
    assert result.json()["status"] == "applied" and remote.applied == 1


@pytest.mark.parametrize(
    "path", [PREFIX + "/pending", PREFIX + "/memories/mem-1", PREFIX + "/" + BODY.request_id]
)
def test_query_and_runtime_routes_fail_closed(http, path):
    assert browser_contract.owner_allowed("GET", path)
    assert not browser_contract.runtime_allowed("GET", path)
    assert http.get(path + "?namespace=other", headers={"X-Pi-Owner-Key": OWNER}).status_code == 422


@pytest.mark.parametrize("change", ["namespace", "destination", "missing"])
def test_companion_namespace_and_endpoint_must_match(http, setup, monkeypatch, change):
    _, client, remote = setup
    memory = (
        None
        if change == "missing"
        else SimpleNamespace(
            agent_id="other" if change == "namespace" else client.agent_id,
            http=SimpleNamespace(
                base_url=httpx.URL(
                    "http://other" if change == "destination" else "http://memorygate/"
                )
            ),
        )
    )
    monkeypatch.setattr(api.app.state, "memory", SimpleNamespace(client=memory))
    result = http.get(PREFIX + "/memories/mem-1", headers={"X-Pi-Owner-Key": OWNER})
    assert result.status_code == 503 and result.json()["detail"]["code"] == "not_configured"
    assert not remote.calls


def test_pending_read_survives_missing_configuration_and_is_content_free(http, setup, monkeypatch):
    _, _, remote = setup
    remote.lose = True
    headers = {"X-Pi-Owner-Key": OWNER}
    http.post(PREFIX, json=BODY.model_dump(), headers=headers)
    monkeypatch.setattr(api.app.state, "memory_corrections", None)
    result = http.get(PREFIX + "/pending", headers=headers)
    assert result.status_code == 200 and BODY.text not in result.text
    assert set(result.json()["pending"]) == {
        "requestId",
        "memoryId",
        "expectedRevision",
        "payloadHash",
        "status",
    }


def test_validation_never_echoes_text_and_raw_body_is_bounded(http):
    headers = {"X-Pi-Owner-Key": OWNER}
    value = http.post(
        PREFIX, json={**BODY.model_dump(), "expected_revision": 2**63}, headers=headers
    )
    assert value.status_code == 422 and BODY.text not in value.text
    value = http.post(PREFIX, content=b"x" * 128001, headers=headers)
    assert value.status_code == 413 and len(value.content) < 200
    assert (
        http.post(PREFIX, json=BODY.model_dump(), headers=headers, params={"x": "y"}).status_code
        == 422
    )


@pytest.mark.parametrize("text", [BODY.text, "\x01" * 16000], ids=["ordinary", "escaped-limit"])
def test_gateway_uses_exact_owner_proof_and_never_runtime_key(tmp_path, monkeypatch, text):
    from gateway import store as auth_module

    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    auth = AuthStore(tmp_path / "auth.db")
    password, origin = "synthetic-owner-password", "https://localhost:8050"
    auth.set_password(password)
    approved = {**BODY.model_dump(), "text": text}
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"schemaVersion": 1})

    with TestClient(
        create_app(
            Config(origin, str(auth.path), "http://pi:8050", "r" * 32, pi_owner_key=OWNER),
            store=auth,
            transport=httpx.MockTransport(upstream),
        ),
        base_url=origin,
    ) as browser:
        path = "/api/control/pi" + PREFIX
        assert browser.get(path + "/pending").status_code == 401
        csrf = browser.get("/auth/session").json()["csrf_token"]
        login = browser.post(
            "/auth/login",
            json={"password": password},
            headers={"Origin": origin, "X-CSRF-Token": csrf},
        )
        headers = {"Origin": origin, "X-CSRF-Token": login.json()["csrf_token"]}
        assert browser.get(path + "/pending").status_code == 200
        assert seen[-1].headers["x-pi-owner-key"] == OWNER
        assert "x-pi-gateway-key" not in seen[-1].headers and "cookie" not in seen[-1].headers
        assert browser.post(path, json=approved, headers=headers).status_code == 428
        proof = browser.post(
            "/auth/verify",
            headers=headers,
            json={
                "password": password,
                "operation": {"method": "POST", "path": path, "body": approved},
            },
        ).json()["verification_token"]
        verified = {**headers, "X-Conker-Verification": proof}
        assert (
            browser.post(
                path, json={**BODY.model_dump(), "text": "changed"}, headers=verified
            ).status_code
            == 428
        )
        response = browser.post(path, json=approved, headers=verified)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert browser.post(path, json=approved, headers=verified).status_code == 428
        assert browser.get(path + "/pending?x=1").status_code == 422
        assert browser.get("/api/pi" + PREFIX + "/pending").status_code == 403
        assert len(seen) == 2


def test_cli_mutations_preserve_forget_and_use_same_narrow_writer():
    correct = mutation._resolve("memory", {"operation": "correct", **BODY.model_dump()})
    assert (
        correct.method == "POST" and correct.path == PREFIX and correct.payload == BODY.model_dump()
    )
    retry = mutation._resolve(
        "memory",
        {
            "operation": "retry-correction",
            "request_id": BODY.request_id,
            "expected_payload_hash": "a" * 64,
        },
    )
    assert retry.path == PREFIX + "/" + BODY.request_id + "/retry"
    assert retry.payload == {"expected_payload_hash": "a" * 64}
    forget = mutation._resolve(
        "memory", {"operation": "forget", **BODY.model_dump(exclude={"text"})}
    )
    assert forget.path == "/memory/forget"
    with pytest.raises(mutation.MutationError):
        mutation._resolve(
            "memory", {"operation": "correct", **BODY.model_dump(), "source_type": "stated"}
        )


@pytest.mark.parametrize(
    "resource,path",
    [
        ("memory-edit:mem-1", PREFIX + "/memories/mem-1"),
        ("memory-edit-pending", PREFIX + "/pending"),
        ("memory-edit-status:" + BODY.request_id, PREFIX + "/" + BODY.request_id),
    ],
)
def test_compiled_cli_inspection_uses_fixed_owner_reads(tmp_path, resource, path):
    from gateway.inspection import inspect_resource

    def upstream(request):
        assert request.method == "GET" and request.url.path == path and not request.url.query
        assert request.headers["X-Pi-Owner-Key"] == OWNER
        assert "X-Pi-Gateway-Key" not in request.headers
        return httpx.Response(200, json={"schemaVersion": 1})

    config = Config(
        "https://localhost:8050",
        str(tmp_path / "auth.db"),
        "http://pi:8050",
        "r" * 32,
        pi_owner_key=OWNER,
    )
    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        assert inspect_resource(config, resource, client) == {"schemaVersion": 1}


@pytest.mark.parametrize(
    "resource",
    [
        "memory-edit:../mem-1",
        "memory-edit:mem-1\n",
        "memory-edit-status:x",
        "memory-edit-status:" + BODY.request_id + "?text=other",
        "memory-edit-pending:other",
    ],
)
def test_compiled_cli_inspection_rejects_unbounded_or_unknown_path(tmp_path, resource):
    from gateway.inspection import InspectionError, inspect_resource

    config = Config(
        "https://localhost:8050",
        str(tmp_path / "auth.db"),
        "http://pi:8050",
        "r" * 32,
        pi_owner_key=OWNER,
    )
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda request: pytest.fail("No request"))
        ) as client,
        pytest.raises(InspectionError),
    ):
        inspect_resource(config, resource, client)
