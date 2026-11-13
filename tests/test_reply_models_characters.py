"""The character routes describe their replies in OpenAPI (D-01), and what they send matches the
description: read before any save, save both ways, read a chosen revision, history, restore,
export and both kinds of import, each 200 reply validated against the model its route names."""

import json
import re
from contextlib import closing

import pytest
import test_characters as flows
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from pi.agents import character_replies as replies
from pi.api import server as api
from pi.api.routes import characters as characters_api
from pi.conversation.store import Store
from pi.core.errors import install

ROUTES = {
    ("get", "/characters/{agent_id}"): replies.CharacterRecord,
    ("put", "/characters/{agent_id}"): replies.CharacterSaved,
    ("post", "/characters/{agent_id}/save"): replies.CharacterSaved,
    ("get", "/characters/{agent_id}/history"): replies.CharacterHistory,
    ("get", "/characters/{agent_id}/export"): replies.CharacterPackage,
    ("post", "/characters/{agent_id}/import"): replies.CharacterImportDraft,
    ("post", "/characters/{agent_id}/restore"): replies.CharacterSaved,
}
OWNER = {"x-owner": "yes"}


def model_for(method, url):
    path = re.sub(r"^/characters/[^/?]+", "/characters/{agent_id}", str(url).split("?")[0])
    return ROUTES.get((method.lower(), path))


class Checked(TestClient):
    seen: list[tuple[str, str]] = []

    def request(self, method, url, *args, **kwargs):
        reply = super().request(method, url, *args, **kwargs)
        model = model_for(method, url)
        if model is not None and reply.status_code == 200:
            model.model_validate_json(reply.text)
            type(self).seen.append((method.lower(), model.__name__))
        return reply


def owner_app(store):
    app = FastAPI()
    install(app)

    def authorize(x_owner: str | None = Header(default=None)):
        if x_owner != "yes":
            raise HTTPException(403, "Owner required")

    app.include_router(characters_api.router(lambda: store, authorize))
    return app


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_each_route_names_its_reply_model_in_openapi(method, path):
    operation = api.app.openapi()["paths"][path][method]
    schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": f"#/components/schemas/{ROUTES[(method, path)].__name__}"}


def test_every_character_reply_matches_its_model(tmp_path):
    Checked.seen = []
    with closing(Store(tmp_path / "pi.db")) as store, Checked(owner_app(store)) as client:
        base = "/characters/companion"
        empty = client.get(base, headers=OWNER)
        assert empty.status_code == 200 and empty.json()["profile"] is None
        body = {"expected_revision": 0, "profile": flows.profile()}
        assert client.put(base, headers=OWNER, json=body).status_code == 200
        body = {"expected_revision": 1, "profile": flows.profile("Changed")}
        assert client.post(f"{base}/save", headers=OWNER, json=body).status_code == 200
        restored = client.post(
            f"{base}/restore", headers=OWNER, json={"expected_revision": 2, "revision": 1}
        )
        assert restored.status_code == 200
        assert client.get(base, headers=OWNER).json()["revision"] == 3
        assert client.get(base, params={"revision": 1}, headers=OWNER).status_code == 200
        history = client.get(f"{base}/history", headers=OWNER).json()["results"]
        assert [row["restored_from"] for row in history] == [None, None, 1]
        package = client.get(f"{base}/export", headers=OWNER).json()
        assert client.get(f"{base}/export", params={"revision": 2}, headers=OWNER).is_success
        card = {"spec": "chara_card_v2", "data": {"name": "Card", "first_mes": "Hello"}}
        for text in (json.dumps(package), json.dumps(card)):
            draft = client.post(f"{base}/import", headers=OWNER, json={"text": text})
            assert draft.status_code == 200
    assert {name for _, name in Checked.seen} == {model.__name__ for model in ROUTES.values()}


def test_the_existing_character_api_flows_match_their_models(tmp_path, monkeypatch):
    monkeypatch.setattr(flows, "TestClient", Checked)
    Checked.seen = []
    with closing(Store(tmp_path / "pi.db")) as store:
        flows.test_owner_api_and_stream_bound(store, monkeypatch)
    assert ("put", "CharacterSaved") in Checked.seen
    assert ("get", "CharacterHistory") in Checked.seen
