"""The owner settings, preferences and model configuration routes describe their replies in
OpenAPI (D-01), and what they send matches the description: read, then save, then read again."""

import hashlib
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.api import server as api
from pi.conversation.store import Store
from pi.models import roles as model_roles
from pi.owner import preferences, settings

ADMIN = "reply-models-admin-key"
ROUTES = {
    "/owner/settings": settings.OwnerSettingsDocument,
    "/owner/preferences": preferences.OwnerPreferencesDocument,
    "/models/configuration": model_roles.ConfigurationDocument,
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", ADMIN, raising=False)
        monkeypatch.setattr(
            api.app.state, "owner_key_hash", hashlib.sha256(b"unused").hexdigest(), raising=False
        )
        yield TestClient(api.app, headers={"X-Pi-Key": ADMIN})


@pytest.mark.parametrize("path", ROUTES)
def test_each_route_names_its_reply_model_in_openapi(path):
    paths = api.app.openapi()["paths"][path]
    for method in ("get", "post"):
        schema = paths[method]["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema == {"$ref": f"#/components/schemas/{ROUTES[path].__name__}"}


def test_the_replies_match_their_models_before_and_after_a_save(client):
    for path, model in ROUTES.items():
        model.model_validate_json(client.get(path).text)
    saved = client.post(
        "/owner/settings",
        json={"expected_revision": 0, "values": {"chats.language_level": "plain"}},
    )
    settings.OwnerSettingsDocument.model_validate_json(saved.text)
    current = client.get("/owner/preferences").json()
    saved = client.post(
        "/owner/preferences",
        json={"expected_revision": current["revision"], "preferences": current["preferences"]},
    )
    preferences.OwnerPreferencesDocument.model_validate_json(saved.text)
    for path, model in ROUTES.items():
        model.model_validate_json(client.get(path).text)
