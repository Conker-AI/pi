"""The gateway passes his Inbox decision on to Pi, only once ToolGate has recorded it."""

import dataclasses
import io
import json

import httpx
import pytest
from test_gateway_mcp import boundary, proof  # noqa: F401 - fixture

from gateway.mutation import MutationError, apply_resource
from tests.test_gateway_mutation import config

PI_OWNER = "p" * 32
DECISION = "/api/owner/requests/req_1/decision"


def with_pi_owner(app):
    app.state.config = dataclasses.replace(app.state.config, pi_owner_key=PI_OWNER)


def test_a_recorded_decision_continues_in_pi(boundary):
    app, client, headers, seen, _ = boundary
    with_pi_owner(app)
    body = {"status": "rejected", "note": "Send it to Sam instead"}
    response = client.post(DECISION, json=body, headers=proof(client, headers, DECISION, body))
    assert response.status_code == 200, response.text
    toolgate, pi = seen
    assert toolgate.url == "http://toolgate.test/v2/owner/requests/req_1/decision"
    assert pi.url == "http://pi.test/approvals/req_1/decided"
    assert pi.headers["x-pi-owner-key"] == PI_OWNER
    assert json.loads(pi.content) == body


def test_a_refused_decision_continues_nothing(boundary):
    app, client, headers, seen, _ = boundary
    with_pi_owner(app)

    def refused(request):
        seen.append(request)
        return httpx.Response(409, json={"detail": {"code": "decision_conflict"}})

    app.state.client = httpx.Client(transport=httpx.MockTransport(refused))
    body = {"status": "approved"}
    response = client.post(DECISION, json=body, headers=proof(client, headers, DECISION, body))
    assert response.status_code == 409
    assert [request.url.host for request in seen] == ["toolgate.test"]


def test_picking_a_tools_buttons_needs_the_password_and_the_list(boundary):
    _, client, headers, seen, _ = boundary
    path = "/api/owner/tools/telegram.send/card"
    assert client.post(path, json={"template": "Cancel"}, headers=headers).status_code == 422
    assert client.post(path, json={"template": "send"}, headers=headers).status_code == 428
    body = {"template": "send"}
    assert (
        client.post(path, json=body, headers=proof(client, headers, path, body)).status_code == 200
    )
    [request] = seen
    assert request.url == "http://toolgate.test/v2/owner/tools/telegram.send/card"
    assert "x-toolgate-execution-key" not in request.headers


def test_the_cli_decision_continues_too():
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"id": "req_1", "status": "approved"})

    payload = {"operation": "decide", "id": "req_1", "status": "approved"}
    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        apply_resource(config(), "approvals", io.BytesIO(json.dumps(payload).encode()), client)
    toolgate, pi = seen
    assert toolgate.url.path == "/v2/owner/requests/req_1/decision"
    assert pi.url.path == "/approvals/req_1/decided"
    assert json.loads(pi.content) == {"status": "approved", "note": ""}


@pytest.mark.parametrize(
    ("payload", "path", "body"),
    [
        (
            {"operation": "answer", "id": "ask_" + "a" * 32, "text": "Yes", "chosen": True},
            "/asks/ask_" + "a" * 32 + "/answer",
            {"text": "Yes", "chosen": True},
        ),
        (
            {"operation": "dismiss", "id": "ask_" + "a" * 32},
            "/asks/ask_" + "a" * 32 + "/dismiss",
            {},
        ),
        (
            {"operation": "decline", "id": "creq_" + "d" * 32, "reply": "Weekly instead"},
            "/change-requests/creq_" + "d" * 32 + "/decline",
            {"reply": "Weekly instead"},
        ),
    ],
)
def test_the_cli_answers_through_the_owner_routes(payload, path, body):
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"id": payload["id"]})

    resource = "asks" if payload["id"].startswith("ask_") else "requests"
    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        apply_resource(config(), resource, io.BytesIO(json.dumps(payload).encode()), client)
    [request] = seen
    assert request.url.path == path and json.loads(request.content) == body


def test_only_a_decline_carries_a_reply():
    payload = {"operation": "approve", "id": "creq_" + "d" * 32, "reply": "x"}
    with pytest.raises(MutationError):
        apply_resource(config(), "requests", io.BytesIO(json.dumps(payload).encode()), None)
