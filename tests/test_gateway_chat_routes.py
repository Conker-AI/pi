"""The v1 chat features through the owner's browser: every route, allowed and refused.

Each read and write in browser_contract.CHAT_READS / CHAT_WRITES has one case here (the coverage
test fails otherwise). Reads need a signed-in session; writes also need CSRF, the exact origin, a
body Pi's own model accepts, no query string and a password proof bound to that exact operation.
Every refusal is checked to have stopped at the gateway: the fake Pi records what reaches it.
"""

import hashlib
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.store import AuthStore
from pi.api.browser_contract import CHAT_READS, CHAT_WRITES, owner_allowed, runtime_allowed

ORIGIN = "https://localhost:8050"
PASSWORD = "local integration testing phrase"
OWNER = "owner-chat-routes-key-" + "o" * 32
CHAT, MESSAGE, ITEM = "ses_0123456789abcdef", "msg_0123456789abcdef", "dec_0123456789abcdef"
REQUEST = "compare_0123456789"
BUDGET = {"contextWindowTokens": 8000, "outputReserveTokens": 1000, "otherInputTokens": 0}

READS = [
    f"/sessions/{CHAT}/output",
    f"/sessions/{CHAT}/title",
    f"/sessions/{CHAT}/branch-history",
    f"/sessions/{CHAT}/response-families/{MESSAGE}",
    f"/sessions/{CHAT}/response-families/{MESSAGE}/judgements",
    f"/sessions/{CHAT}/response-families/{MESSAGE}/votes",
    f"/sessions/{CHAT}/response-retries/{REQUEST}",
    f"/sessions/{CHAT}/messages/{MESSAGE}/fact-checks",
    f"/context/{CHAT}/items",
    f"/context/{CHAT}/compaction",
]
WRITES = [
    (
        f"/sessions/{CHAT}/output",
        {"expected_revision": 0, "output": {"format": "list", "length": "short"}},
    ),
    (f"/sessions/{CHAT}/title", {"expected_revision": 0, "title": "Trip plan"}),
    (f"/sessions/{CHAT}/title/auto", {"expected_revision": 0}),
    (f"/sessions/{CHAT}/messages/{MESSAGE}/retry", {"request_id": REQUEST, "model_id": "m-1"}),
    (f"/sessions/{CHAT}/messages/{MESSAGE}/fact-check", {"request_id": REQUEST}),
    (
        f"/sessions/{CHAT}/messages/{MESSAGE}/fork",
        {"request_id": REQUEST, "expected_settings_revision": 0, "expected_context_revision": 0},
    ),
    (
        f"/sessions/{CHAT}/response-families/{MESSAGE}/select",
        {"message_id": MESSAGE, "expected_revision": 1},
    ),
    (
        f"/sessions/{CHAT}/response-families/{MESSAGE}/judge",
        {"request_id": REQUEST, "rubric": "Which answer is accurate?"},
    ),
    (f"/sessions/{CHAT}/response-families/{MESSAGE}/vote", {"message_id": MESSAGE, "vote": "up"}),
    (
        f"/context/{CHAT}",
        {
            "expected_revision": 0,
            "policy": {
                "sessionInstructions": "Short answers.",
                "messagePolicies": {MESSAGE: "keep-exact"},
                "budget": BUDGET,
            },
        },
    ),
    (
        f"/context/{CHAT}/fork",
        {"expected_revision": 1, "summary": "We chose the coast route.", "request_id": REQUEST},
    ),
    (f"/context/{CHAT}/summary", {"expected_revision": 0, "summary": "Coast route."}),
    (f"/context/{CHAT}/summary/restore", {"expected_revision": 2, "revision": 1}),
    (f"/context/{CHAT}/summary/draft", {"focus": "the route"}),
    (
        f"/context/{CHAT}/compact",
        {
            "expected_policy_revision": 1,
            "expected_summary_revision": 0,
            "summary": "Earlier: the coast route.",
            "covers": [MESSAGE],
        },
    ),
    (
        f"/context/{CHAT}/items",
        {"expected_revision": 0, "kind": "decision", "text": "Take the coast route."},
    ),
    (f"/context/{CHAT}/items/{ITEM}/state", {"expected_revision": 1, "state": "withdrawn"}),
]
# A wrong value of the right shape that Pi's model refuses, per write.
WRONG = {
    "output": {"output": {"format": "poem"}},
    "title": {"title": ""},
    "auto": {"expected_revision": -1},
    "retry": {"request_id": "short"},
    "fact-check": {"request_id": "has spaces in it ok?"},
    "fork": {"expected_settings_revision": "0"},
    "select": {"expected_revision": 0},
    "judge": {"rubric": "x" * 2001},
    "vote": {"vote": "maybe"},
    CHAT: {"policy": {"sessionInstructions": "", "messagePolicies": {}, "budget": {}}},
    "summary": {"summary": "x" * 16001},
    "restore": {"revision": "1"},
    "draft": {"focus": "x" * 501},
    "compact": {"covers": []},
    "items": {"kind": "note"},
    "state": {"state": "deleted"},
}


def name(path: str) -> str:
    """A test id: the path without its ids or parameters, e.g. context-items-state."""
    ids = (CHAT, MESSAGE, ITEM, REQUEST)
    return "-".join(
        part for part in path.split("/") if part and part not in ids and not part.startswith("{")
    )


def wrong(path: str, body: dict) -> dict:
    return {**body, **WRONG[path.rsplit("/", 1)[1]]}


def malformed(path: str):
    """Every id in the path made invalid one way or another; a trailing segment too."""
    for good, bads in (
        (CHAT, ("ses_0123456789ABCDEF", "ses_0123", "sample_0123456789abcdef", "ses_01234567..")),
        (MESSAGE, ("msg_0123456789abcdeg", "msg_", "impmsg_" + "0" * 32)),
        (ITEM, ("item_0123456789abcdef", "dec_0123")),
        (REQUEST, ("short", "x" * 129, "compare_01234567!8")),
    ):
        if good in path:
            for bad in bads:
                yield path.replace(good, bad)
    yield path + "/x"


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    config = Config(ORIGIN, str(auth.path), "http://pi:8050", "r" * 32, pi_owner_key=OWNER)
    app = create_app(config, store=auth, transport=httpx.MockTransport(upstream))
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = client.get("/auth/session").json()["csrf_token"]
        login = client.post(
            "/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}

        def proof(path, body):
            answer = client.post(
                "/auth/verify",
                headers=headers,
                json={
                    "password": PASSWORD,
                    "operation": {"method": "POST", "path": path, "body": body},
                },
            )
            assert answer.status_code == 200, answer.text
            return {**headers, "X-Conker-Verification": answer.json()["verification_token"]}

        with TestClient(app, base_url=ORIGIN) as anonymous:
            yield client, anonymous, headers, proof, seen


def test_every_chat_route_has_one_case_and_none_is_a_runtime_route():
    for patterns, paths in ((CHAT_READS, READS), (CHAT_WRITES, [path for path, _ in WRITES])):
        assert len(patterns) == len(paths) == len(set(paths))
        for pattern in patterns:
            assert [path for path in paths if re.fullmatch(pattern, path)], pattern
        for path in paths:
            assert [pattern for pattern in patterns if re.fullmatch(pattern, path)], path
    for path in READS:
        assert owner_allowed("GET", path) and not runtime_allowed("GET", path)
        assert not owner_allowed("POST", path) or path in dict(WRITES)
    for path, _ in WRITES:
        assert owner_allowed("POST", path) and not runtime_allowed("POST", path)
        assert path.rsplit("/", 1)[1] in WRONG


@pytest.mark.parametrize("path", READS, ids=[name(path) for path in READS])
def test_owner_reads_through_the_control_key(gateway, path):
    client, anonymous, _, _, seen = gateway
    assert anonymous.get("/api/control/pi" + path).status_code == 401
    assert (
        anonymous.get("/api/control/pi" + path, headers={"X-Pi-Owner-Key": OWNER}).status_code
        == 401
    )
    assert client.get("/api/pi" + path).status_code == 403
    for bad in malformed(path):
        assert client.get("/api/control/pi" + bad).status_code == 403, bad
    assert client.get("/api/control/pi" + path + "?debug=1").status_code == 422
    assert seen == []
    answer = client.get("/api/control/pi" + path)
    assert answer.status_code == 200 and answer.json() == {"ok": True}
    assert len(seen) == 1 and seen[0].url.path == path and seen[0].method == "GET"
    assert seen[0].headers["x-pi-owner-key"] == OWNER
    assert "x-pi-gateway-key" not in seen[0].headers and "cookie" not in seen[0].headers


def test_branch_history_takes_only_its_page_parameters(gateway):
    client, _, _, _, seen = gateway
    path = f"/api/control/pi/sessions/{CHAT}/branch-history"
    assert client.get(path + "?limit=50&cursor=abc").status_code == 200
    assert seen[-1].url.params["limit"] == "50" and seen[-1].url.params["cursor"] == "abc"
    for query in ("?limit=1&limit=2", "?limit=5&owner=1", "?=", "?limit"):
        assert client.get(path + query).status_code == 422, query
    assert len(seen) == 1


@pytest.mark.parametrize("path,body", WRITES, ids=[name(path) for path, _ in WRITES])
def test_owner_write_needs_session_csrf_exact_body_and_a_bound_proof(gateway, path, body):
    client, anonymous, headers, proof, seen = gateway
    target = "/api/control/pi" + path
    # No session, a service key instead of a session, missing or wrong CSRF, a foreign origin.
    assert anonymous.post(target, json=body, headers=headers).status_code == 401
    assert anonymous.post(target, json=body, headers={"X-Pi-Owner-Key": OWNER}).status_code == 401
    assert client.post(target, json=body, headers={"Origin": ORIGIN}).status_code == 403
    assert (
        client.post(
            target, json=body, headers={"Origin": ORIGIN, "X-CSRF-Token": "x" * 43}
        ).status_code
        == 403
    )
    foreign = {**headers, "Origin": "https://evil.example"}
    assert client.post(target, json=body, headers=foreign).status_code == 403
    # Not a runtime route, and no id but the exact one.
    assert client.post("/api/pi" + path, json=body, headers=headers).status_code == 403
    for bad in malformed(path):
        assert client.post("/api/control/pi" + bad, json=body, headers=headers).status_code == 403
    # Unknown fields, wrong values, a query string: refused even with a valid proof for them.
    for refused in ({**body, "extra": True}, wrong(path, body), {**body, "request_id": None}):
        if refused == body:
            continue
        checked = proof(target, refused)
        assert client.post(target, json=refused, headers=checked).status_code == 422, refused
    assert client.post(target, json=[body], headers=headers).status_code == 422
    assert client.post(target + "?force=1", json=body, headers=headers).status_code == 422
    assert seen == []
    # A valid body without a proof, then with a proof bound to another body.
    assert client.post(target, json=body, headers=headers).status_code == 428
    other = proof(target, {**body, "extra": True})
    assert client.post(target, json=body, headers=other).status_code == 428
    assert seen == []
    checked = proof(target, body)
    answer = client.post(target, json=body, headers=checked)
    assert answer.status_code == 200 and answer.json() == {"ok": True}
    assert len(seen) == 1 and seen[0].url.path == path and seen[0].method == "POST"
    assert json.loads(seen[0].content) == body
    assert seen[0].headers["x-pi-owner-key"] == OWNER
    assert "x-pi-gateway-key" not in seen[0].headers and "cookie" not in seen[0].headers
    assert "x-conker-verification" not in seen[0].headers
    assert client.post(target, json=body, headers=checked).status_code == 428
    assert len(seen) == 1
