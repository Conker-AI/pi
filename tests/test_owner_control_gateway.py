import hashlib

import httpx
from fastapi.testclient import TestClient

from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.store import AuthStore
from pi.browser_contract import owner_allowed, runtime_allowed

ORIGIN = "https://localhost:8050"
PASSWORD = "local integration testing phrase"
OWNER = "owner-control-test-key-" + "o" * 32


def test_separate_owner_transport_exact_proof_and_no_runtime_escalation(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_module, "password_hash", lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest())
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    seen = []
    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"revision": 1, "configuration": None})
    config = Config(ORIGIN, str(auth.path), "http://pi:8050", "r" * 32, pi_owner_key=OWNER)
    with TestClient(create_app(config, store=auth, transport=httpx.MockTransport(upstream)), base_url=ORIGIN) as client:
        path = "/api/control/pi/models/configuration"
        assert client.get(path).status_code == 401
        csrf = client.get("/auth/session").json()["csrf_token"]
        login = client.post("/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN, "X-CSRF-Token": csrf})
        headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
        assert client.get(path).status_code == 200
        assert seen[-1].headers["x-pi-owner-key"] == OWNER
        assert "x-pi-gateway-key" not in seen[-1].headers
        assert "cookie" not in seen[-1].headers
        body = {"expected_revision": 0, "configuration": {"test": True}}
        assert client.post(path, json=body, headers=headers).status_code == 428
        proof = client.post("/auth/verify", headers=headers, json={"password": PASSWORD,
            "operation": {"method": "POST", "path": path, "body": body}})
        checked = {**headers, "X-Conker-Verification": proof.json()["verification_token"]}
        assert client.post(path, json={**body, "expected_revision": 1}, headers=checked).status_code == 428
        assert client.post(path, json=body, headers=checked).status_code == 200
        assert client.post(path, json=body, headers=checked).status_code == 428
        assert len(seen) == 2
        assert client.get("/api/pi/models/configuration").status_code == 403
        assert client.get("/api/control/pi/agents").status_code == 403
        assert client.get("/api/control/pi/vault").status_code == 403
    assert not runtime_allowed("POST", "/models/configuration")
    assert owner_allowed("POST", "/models/configuration")
    assert not owner_allowed("POST", "/sessions")


def test_pi_owner_key_cannot_be_used_as_runtime_or_recovery(monkeypatch):
    from pi.api import app
    owner_hash = hashlib.sha256(OWNER.encode()).hexdigest()
    monkeypatch.setattr(app.state, "admin_key", "admin-test-" + "a" * 32, raising=False)
    monkeypatch.setattr(app.state, "gateway_key_hash", hashlib.sha256(("r" * 32).encode()).hexdigest(), raising=False)
    monkeypatch.setattr(app.state, "owner_key_hash", owner_hash, raising=False)
    # No lifespan/storage needed: denied calls must never reach the store.
    client = TestClient(app)
    assert client.get("/models/configuration", headers={"X-Pi-Gateway-Key": "r" * 32}).status_code == 401
    assert client.get("/agents", headers={"X-Pi-Owner-Key": OWNER}).status_code == 401
    assert client.get("/sessions", headers={"X-Pi-Owner-Key": OWNER}).status_code == 401
