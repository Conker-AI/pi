"""A decision asks for his password only when ToolGate says the request is serious (level 3)."""

import httpx
import pytest
from test_gateway_mcp import boundary, proof  # noqa: F401 - fixture

from gateway import approval_levels as levels

DECISION = "/api/owner/requests/req_1/decision"
VIEW = "http://toolgate.test/v2/owner/requests/req_1"


def toolgate(app, seen, view):
    """ToolGate answers the owner view with `view` (a status code, a body or an exception)."""

    def answer(request):
        seen.append(request)
        if request.method == "GET":
            if isinstance(view, Exception):
                raise view
            if isinstance(view, int):
                return httpx.Response(view, json={"detail": "no"})
            if isinstance(view, str):
                return httpx.Response(200, text=view)
            return httpx.Response(200, json=view)
        return httpx.Response(200, json={"id": "req_1", "status": "approved"})

    app.state.client = httpx.Client(transport=httpx.MockTransport(answer))


def at(level):
    return {"id": "req_1", "approval_level": level}


@pytest.mark.parametrize("level", [1, 2])
def test_a_normal_or_outward_yes_needs_the_session_only(boundary, level):
    app, client, headers, seen, _ = boundary
    toolgate(app, seen, at(level))
    response = client.post(DECISION, json={"status": "approved"}, headers=headers)
    assert response.status_code == 200, response.text
    read, decided = seen
    assert read.method == "GET" and str(read.url) == VIEW
    assert read.headers["x-toolgate-owner-key"] and "cookie" not in read.headers
    assert decided.method == "POST" and str(decided.url) == VIEW + "/decision"


@pytest.mark.parametrize(
    "view",
    [
        at(3),
        at(None),
        at("1"),
        at(True),
        at(1.0),
        at(0),
        at(4),
        {"approval_level": 1},  # An older ToolGate or another request's view.
        {"id": "req_2", "approval_level": 1},
        [at(1)],
        "not json",
        404,
        503,
        httpx.ConnectError("down"),
        httpx.ReadTimeout("slow"),
    ],
)
def test_a_serious_or_unreadable_level_asks_for_the_password(boundary, view):
    app, client, headers, seen, _ = boundary
    toolgate(app, seen, view)
    body = {"status": "approved"}
    response = client.post(DECISION, json=body, headers=headers)
    assert response.status_code == 428
    assert response.json()["detail"]["code"] == "verification_required"
    assert [request.method for request in seen] == ["GET"]
    seen.clear()
    assert (
        client.post(DECISION, json=body, headers=proof(client, headers, DECISION, body)).status_code
        == 200
    )
    assert [request.method for request in seen] == ["GET", "POST"]


@pytest.mark.parametrize("status", ["rejected", "dismissed"])
def test_a_no_runs_nothing_so_it_needs_no_password_even_when_serious(boundary, status):
    app, client, headers, seen, _ = boundary
    toolgate(app, seen, at(3))
    assert client.post(DECISION, json={"status": status}, headers=headers).status_code == 200
    # It does not even ask ToolGate for the level.
    assert [request.method for request in seen] == ["POST"]


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_every_decision_still_needs_the_signed_in_session(boundary, status):
    app, client, headers, seen, _ = boundary
    toolgate(app, seen, at(1))
    body = {"status": status}
    for wrong in (
        {},
        {**headers, "Origin": "https://elsewhere.test"},
        {**headers, "X-CSRF-Token": "x"},
    ):
        assert client.post(DECISION, json=body, headers=wrong).status_code == 403
    assert client.post(DECISION + "?level=1", json=body, headers=headers).status_code == 422
    assert client.post(DECISION, json={**body, "level": 1}, headers=headers).status_code == 422
    assert not seen
    client.cookies.clear()
    assert client.post(DECISION, json=body, headers=headers).status_code == 401
    assert not seen


def test_a_level_proof_is_single_use_and_bound_to_the_exact_decision(boundary):
    app, client, headers, seen, _ = boundary
    toolgate(app, seen, at(3))
    body = {"status": "approved", "note": "pay the invoice"}
    checked = proof(client, headers, DECISION, body)
    assert client.post(DECISION, json={"status": "approved"}, headers=checked).status_code == 428
    assert client.post(DECISION, json=body, headers=checked).status_code == 200
    assert client.post(DECISION, json=body, headers=checked).status_code == 428


def test_only_a_yes_at_level_three_or_unknown_needs_the_password():
    assert not levels.needs_password("approved", 1)
    assert not levels.needs_password("approved", 2)
    assert levels.needs_password("approved", 3)
    assert levels.needs_password("approved", 0)
    for status in ("rejected", "dismissed"):
        assert not levels.needs_password(status, 3)
