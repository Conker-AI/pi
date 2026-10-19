"""Every refusal reaches the client as {"detail": {"code", "message"}} with its status."""

from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.api import server as api
from pi.conversation.store import Store

ADMIN = {"X-Pi-Key": "synthetic-admin"}


@pytest.fixture
def http(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        monkeypatch.setattr(api.app.state, "store", store, raising=False)
        monkeypatch.setattr(api.app.state, "admin_key", "synthetic-admin", raising=False)
        yield store, TestClient(api.app)


@pytest.mark.parametrize(
    "method,path,body,status,code,message",
    [
        ("GET", "/context/ses_missing", None, 404, "unavailable", "Conversation unavailable."),
        ("GET", "/tasks/tsk_missing", None, 404, "not_found", "Task not found."),
        ("GET", f"/jobs/job_{'0' * 32}", None, 404, "not_found", "Job not found."),
        (
            "GET",
            "/sessions/{session}/attachments/att_missing",
            None,
            404,
            "not_found",
            "Attachment not found in this session.",
        ),
        (
            "GET",
            "/sessions/{session}/response-families/msg_missing/judgements",
            None,
            404,
            "not_found",
            "Response family not found.",
        ),
        (
            "POST",
            "/setup/choices/companion",
            {"requestId": "req_shape", "choice": "skip", "expectedRevision": 0},
            422,
            "invalid_choice",
            "Companion accepts its current default; optional services use include or skip.",
        ),
        (
            "GET",
            "/sessions/ses_missing/output",
            None,
            404,
            "not_found",
            "Conversation unavailable.",
        ),
    ],
)
def test_refusals_share_one_shape(http, method, path, body, status, code, message):
    store, client = http
    path = path.format(session=store.create_session())
    response = client.request(method, path, headers=ADMIN, json=body)

    assert response.status_code == status
    assert response.json() == {"detail": {"code": code, "message": message}}
