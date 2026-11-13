"""The chat name and output routes describe their replies in OpenAPI (D-01), and what they send
matches the description: read, save, read again."""

import hashlib
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.api import server as api
from pi.conversation import naming, output_settings
from pi.conversation.store import Store

ADMIN = "reply-models-chat-admin"
ROUTES = {
    ("get", "/sessions/{session_id}/title"): naming.TitleDocument,
    ("post", "/sessions/{session_id}/title"): naming.TitleDocument,
    ("post", "/sessions/{session_id}/title/auto"): naming.TitleDocument,
    ("get", "/sessions/{session_id}/output"): output_settings.OutputDocument,
    ("post", "/sessions/{session_id}/output"): output_settings.OutputDocument,
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", ADMIN, raising=False)
        monkeypatch.setattr(
            api.app.state, "owner_key_hash", hashlib.sha256(b"unused").hexdigest(), raising=False
        )
        yield store, TestClient(api.app, headers={"X-Pi-Key": ADMIN})


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_each_route_names_its_reply_model_in_openapi(method, path):
    reply = api.app.openapi()["paths"][path][method]["responses"]["200"]
    schema = reply["content"]["application/json"]["schema"]
    assert schema == {"$ref": f"#/components/schemas/{ROUTES[(method, path)].__name__}"}


def test_the_replies_match_their_models(client):
    store, http = client
    chat = store.create_session()
    title, output = f"/sessions/{chat}/title", f"/sessions/{chat}/output"
    assert naming.TitleDocument.model_validate_json(http.get(title).text).source is None
    saved = http.post(title, json={"expected_revision": 0, "title": "Trip plans"})
    assert naming.TitleDocument.model_validate_json(saved.text).source == "owner"
    naming.TitleDocument.model_validate_json(http.get(title).text)
    # The automatic name is saved and read back by the same code as a rename.
    with store._connect() as db:
        db.execute(
            "UPDATE session_titles SET source='auto', model_id='m' WHERE session_id=?", (chat,)
        )
    assert naming.TitleDocument.model_validate_json(http.get(title).text).modelId == "m"

    output_settings.OutputDocument.model_validate_json(http.get(output).text)
    body = {"expected_revision": 0, "output": {"format": "table", "level": "plain"}}
    output_settings.OutputDocument.model_validate_json(http.post(output, json=body).text)
    output_settings.OutputDocument.model_validate_json(http.get(output).text)
