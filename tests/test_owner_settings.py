"""The owner's settings document: preferences by registry id, revision-checked, shape-checked."""

import hashlib
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.api import server as api
from pi.api.browser_contract import owner_allowed, session_only_write
from pi.conversation.store import Store
from pi.owner import settings

OWNER = "owner-settings-owner-key-" + "o" * 32


@pytest.fixture()
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def test_a_new_install_has_an_empty_document(store):
    assert settings.load(store) == {"schemaVersion": 1, "revision": 0, "values": {}}
    assert settings.value(store, "appearance.accent", "neutral") == "neutral"


def test_saves_are_revision_checked_and_replace_the_document(store):
    saved = settings.save(
        store, settings.Update(expected_revision=0, values={"appearance.accent": "blue"})
    )
    assert saved["revision"] == 1 and settings.value(store, "appearance.accent") == "blue"
    with pytest.raises(settings.RevisionConflict) as stale:
        settings.save(store, settings.Update(expected_revision=0, values={}))
    assert stale.value.current_revision == 1
    # A value left out goes back to its default.
    settings.save(store, settings.Update(expected_revision=1, values={"chats.names.auto": False}))
    assert settings.load(store)["values"] == {"chats.names.auto": False}


@pytest.mark.parametrize(
    "values",
    [
        {"Appearance.Accent": "blue"},
        {"accent": "blue"},
        {"appearance.accent": float("nan")},
        {"appearance.notes": "x" * (settings.MAX_VALUE_BYTES + 1)},
        {"appearance.deep": {"a": {"b": {"c": {"d": {"e": 1}}}}}},
        {f"group.item_{index}": index for index in range(settings.MAX_SETTINGS + 1)},
    ],
)
def test_only_setting_ids_and_bounded_plain_values_are_kept(store, values):
    with pytest.raises(ValueError):
        settings.save(store, settings.Update(expected_revision=0, values=values))
    assert settings.load(store)["revision"] == 0


def test_the_owner_reads_and_saves_it_without_a_password_and_the_chat_key_cannot(
    store, monkeypatch
):
    assert owner_allowed("GET", "/owner/settings") and owner_allowed("POST", "/owner/settings")
    assert session_only_write("POST", "/owner/settings")
    # Preferences can change what Conker spends and when it may act: they keep the password.
    assert owner_allowed("POST", "/owner/preferences") and not session_only_write(
        "POST", "/owner/preferences"
    )
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "settings-admin", raising=False)
    monkeypatch.setattr(
        api.app.state, "gateway_key_hash", hashlib.sha256(b"runtime_key").hexdigest(), raising=False
    )
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    client = TestClient(api.app)
    owner = {"X-Pi-Owner-Key": OWNER}
    body = {"expected_revision": 0, "values": {"appearance.accent": "indigo"}}
    assert (
        client.post(
            "/owner/settings", json=body, headers={"X-Pi-Gateway-Key": "runtime_key"}
        ).status_code
        == 401
    )
    saved = client.post("/owner/settings", json=body, headers=owner)
    assert saved.status_code == 200 and saved.json()["revision"] == 1
    assert client.get("/owner/settings", headers=owner).json()["values"] == {
        "appearance.accent": "indigo"
    }
    stale = client.post("/owner/settings", json=body, headers=owner)
    assert stale.status_code == 409 and stale.json()["detail"]["current_revision"] == 1
    bad = client.post(
        "/owner/settings", json={"expected_revision": 1, "values": {"Bad Id": 1}}, headers=owner
    )
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_settings"
