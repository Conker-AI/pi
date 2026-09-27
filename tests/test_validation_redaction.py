import hashlib

from fastapi.testclient import TestClient

from pi import api


def test_validation_errors_never_reflect_submitted_secret(monkeypatch):
    secret = "secret-sentinel-never-return-4729"
    owner = "owner-validation-test-" + "o" * 32
    monkeypatch.setattr(api.app.state, "admin_key", "admin-validation-test-" + "a" * 32, raising=False)
    monkeypatch.setattr(
        api.app.state,
        "owner_key_hash",
        hashlib.sha256(owner.encode()).hexdigest(),
        raising=False,
    )

    response = TestClient(api.app).post(
        "/models/configuration",
        headers={"X-Pi-Owner-Key": owner},
        json={"expected_revision": 0, "configuration": None, "api_key": secret},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Request validation failed."}
    assert secret not in response.text
