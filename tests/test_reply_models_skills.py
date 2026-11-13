"""The skill routes and `POST /tool-drafts` describe their replies in OpenAPI (D-01), and what
they send matches the description: each 200 reply is validated against the model its route
names."""

import hashlib
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from test_skills import document

from pi.agents import change_requests
from pi.agents.skill_replies import SkillDetail, SkillList
from pi.api import server as api
from pi.conversation.store import Store

OWNER = "reply-models-skills-owner-key-" + "o" * 32
VIEW = change_requests.ChangeRequestView
ROUTES = {
    ("get", "/skills"): SkillList,
    ("get", "/skills/{identity}"): SkillDetail,
    ("post", "/skills/import"): SkillDetail,
    ("post", "/skills/{identity}/state"): SkillDetail,
    ("post", "/tool-drafts"): VIEW,
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        for name, value in (
            ("store", store),
            ("admin_key", "reply-models-skills-admin"),
            ("owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest()),
            ("loop", None),
            ("toolgate", None),
        ):
            monkeypatch.setattr(api.app.state, name, value, raising=False)
        yield store, TestClient(api.app, headers={"X-Pi-Owner-Key": OWNER})


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_each_route_names_its_reply_model_in_openapi(method, path):
    reply = api.app.openapi()["paths"][path][method]["responses"]["200"]
    schema = reply["content"]["application/json"]["schema"]
    assert schema == {"$ref": f"#/components/schemas/{ROUTES[(method, path)].__name__}"}


def test_the_skill_replies_match_their_models(client):
    _, http = client
    assert SkillList.model_validate_json(http.get("/skills").text).results == []
    first = http.post("/skills/import", json={"markdown": document(), "expected_revision": 0})
    assert first.status_code == 200, first.text
    assert SkillDetail.model_validate_json(first.text).revision == 1
    other = {"markdown": document("notes"), "expected_revision": 0}
    SkillDetail.model_validate_json(http.post("/skills/import", json=other).text)
    replaced = {"markdown": document(body="Second body."), "expected_revision": 1}
    SkillDetail.model_validate_json(http.post("/skills/import", json=replaced).text)
    off = http.post("/skills/review/state", json={"expected_revision": 2, "enabled": False})
    assert SkillDetail.model_validate_json(off.text).enabled is False
    shown = SkillDetail.model_validate_json(http.get("/skills/review").text)
    assert shown.revision == 3 and shown.markdown == document(body="Second body.")
    listed = SkillList.model_validate_json(http.get("/skills").text).results
    assert [skill.id for skill in listed] == ["notes", "review"]


def test_the_tool_draft_reply_matches_its_model(client):
    _, http = client
    body = {"description": "Search the web for a topic", "request_id": "tool-draft-reply-01"}
    first = http.post("/tool-drafts", json=body)
    assert first.status_code == 200, first.text
    request = VIEW.model_validate_json(first.text)
    assert request.kind == "tool_draft" and request.sessionId is None
    # The same request_id gives back the stored request, read again.
    again = VIEW.model_validate_json(http.post("/tool-drafts", json=body).text)
    assert again == request
