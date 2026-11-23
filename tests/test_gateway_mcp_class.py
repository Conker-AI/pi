"""An imported MCP tool's read class from the browser: Observe needs the owner's exact password
proof, Act outward the session; nothing but the session's owner channel reaches ToolGate."""

import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import store as auth_module
from gateway.api import Config, create_app
from gateway.store import AuthStore
from gateway.toolgate_contract import owner_editor_allowed, owner_request_allowed

ORIGIN = "https://localhost:8050"
PASSWORD = "synthetic MCP class verification phrase"
OWNER_KEY = "mcp-class-owner-" + "o" * 32
EXECUTION_KEY = "tgx_class_" + "e" * 32
PATH = "/api/owner/mcp/tools/mcp.weather/class"
UPSTREAM = "http://toolgate.test/v2/owner/mcp/tools/mcp.weather/class"
OBSERVE = {"read_class": "observe", "expected_version": 3}
ACT_OUTWARD = {"read_class": "act_outward", "expected_version": 3}
SAVED = {"schemaVersion": 1, "tool": {"id": "mcp.weather", "read_class": "observe", "version": 4}}
SECRET = "upstream-private-secret"
# Keys a caller other than the signed-in owner might hold: the agent's (chat runtime) keys and
# Pi's recovery key. None of them is a session, and none is ever passed on.
OTHER_KEYS = {
    "X-Pi-Key": "pi-recovery-key",
    "X-Pi-Gateway-Key": "pi-agent-key",
    "X-ToolGate-Execution-Key": "browser-execution",
    "X-ToolGate-Key": "browser-agent",
    "X-ToolGate-Owner-Key": "browser-owner",
    "Authorization": "Bearer browser",
}


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "password_hash",
        lambda value, salt: hashlib.sha256(salt + value.encode()).hexdigest(),
    )
    auth = AuthStore(tmp_path / "auth.db")
    auth.set_password(PASSWORD)
    seen, reply = [], {"status": 200, "json": SAVED}

    def upstream(request):
        seen.append(request)
        if reply["status"] == "timeout":
            raise httpx.ReadTimeout(SECRET)
        return httpx.Response(reply["status"], json=reply["json"], headers={"X-Private": SECRET})

    config = Config(
        ORIGIN,
        str(auth.path),
        "http://pi.test",
        "r" * 32,
        toolgate_url="http://toolgate.test",
        owner_key=OWNER_KEY,
        toolgate_execution_key=EXECUTION_KEY,
    )
    app = create_app(config, store=auth, transport=httpx.MockTransport(upstream))
    with TestClient(app, base_url=ORIGIN) as client:
        csrf = client.get("/auth/session").json()["csrf_token"]
        login = client.post(
            "/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        )
        headers = {"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
        yield client, headers, seen, reply


def proof(client, headers, body, path=PATH):
    response = client.post(
        "/auth/verify",
        headers=headers,
        json={"password": PASSWORD, "operation": {"method": "POST", "path": path, "body": body}},
    )
    assert response.status_code == 200, response.text
    return {**headers, "X-Conker-Verification": response.json()["verification_token"]}


def assert_relayed_on_the_owner_channel_alone(seen, response, body):
    assert response.status_code == 200, response.text
    assert response.json() == SAVED
    assert len(seen) == 1
    assert str(seen[0].url) == UPSTREAM
    assert json.loads(seen[0].content) == body
    assert seen[0].headers["x-toolgate-owner-key"] == OWNER_KEY
    for header in ("x-toolgate-execution-key", "x-toolgate-key", "x-pi-key", "x-pi-gateway-key"):
        assert header not in seen[0].headers
    assert "cookie" not in seen[0].headers and "x-conker-verification" not in seen[0].headers
    assert "authorization" not in seen[0].headers
    assert PASSWORD not in seen[0].content.decode()
    assert "x-private" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_observe_without_the_password_is_428_and_never_reaches_toolgate(gateway):
    client, headers, seen, _ = gateway
    response = client.post(PATH, json=OBSERVE, headers=headers)
    assert response.status_code == 428
    assert response.json()["detail"]["code"] == "verification_required"
    assert not seen


def test_observe_with_the_exact_password_proof_is_relayed_once(gateway):
    client, headers, seen, _ = gateway
    # A proof for Act outward, another version or another tool does not carry over to Observe.
    for body, path in (
        (ACT_OUTWARD, PATH),
        ({**OBSERVE, "expected_version": 2}, PATH),
        (OBSERVE, "/api/owner/mcp/tools/mcp.other/class"),
        ({"enabled": True, "expected_digest": "b" * 64}, "/api/owner/mcp/tools/mcp.weather/access"),
    ):
        assert (
            client.post(PATH, json=OBSERVE, headers=proof(client, headers, body, path)).status_code
            == 428
        )
    assert not seen
    checked = proof(client, headers, OBSERVE)
    assert_relayed_on_the_owner_channel_alone(
        seen, client.post(PATH, json=OBSERVE, headers=checked), OBSERVE
    )
    # Single use: the same proof cannot send it twice.
    assert client.post(PATH, json=OBSERVE, headers=checked).status_code == 428
    assert len(seen) == 1


def test_act_outward_is_relayed_with_the_session_alone(gateway):
    client, headers, seen, _ = gateway
    response = client.post(PATH, json=ACT_OUTWARD, headers=headers)
    assert_relayed_on_the_owner_channel_alone(seen, response, ACT_OUTWARD)


@pytest.mark.parametrize("body", [OBSERVE, ACT_OUTWARD])
def test_agent_and_recovery_keys_are_refused(gateway, body):
    client, headers, seen, _ = gateway
    session = dict(client.cookies)
    client.cookies.clear()
    # No signed-in session: whatever key the caller holds, the gateway refuses.
    assert client.post(PATH, json=body, headers={**headers, **OTHER_KEYS}).status_code == 401
    assert client.post(PATH, json=body, headers=OTHER_KEYS).status_code in {401, 403}
    assert not seen
    # Signed in, a key never stands in for the password, and only the owner channel is used.
    client.cookies.update(session)
    response = client.post(PATH, json=body, headers={**headers, **OTHER_KEYS})
    if body is OBSERVE:
        assert response.status_code == 428
        assert not seen
    else:
        assert_relayed_on_the_owner_channel_alone(seen, response, body)


def test_the_host_channel_cannot_reach_the_class_route():
    # The CLI's ToolGate owner channel has its own fixed list; Observe is never on it.
    target = "/v2/owner/mcp/tools/mcp.weather/class"
    assert not owner_request_allowed("POST", target)
    assert not owner_editor_allowed("POST", target)
    assert not owner_editor_allowed("POST", target, execution=True)


@pytest.mark.parametrize(
    "body",
    [
        {"read_class": "OBSERVE", "expected_version": 3},
        {"read_class": "auto", "expected_version": 3},
        {"read_class": "ask", "expected_version": 3},
        {"read_class": True, "expected_version": 3},
        {"read_class": ["observe"], "expected_version": 3},
        {"read_class": "observe"},
        {"read_class": "observe", "expected_version": 0},
        {"read_class": "observe", "expected_version": True},
        {"read_class": "observe", "expected_version": "3"},
        {"read_class": "observe", "expected_version": 3.0},
        {"read_class": "observe", "expected_version": 2**53},
        {**OBSERVE, "authorization": "auto"},
        {},
    ],
)
def test_anything_but_the_two_classes_is_refused_before_toolgate(gateway, body):
    client, headers, seen, _ = gateway
    response = client.post(PATH, json=body, headers=headers)
    assert response.status_code == 422
    assert SECRET not in response.text and "OBSERVE" not in response.text
    assert not seen


@pytest.mark.parametrize("identity", ["shell", "mcp.AA", "mcp.a", "mcp.a_b", "mcp." + "x" * 76])
def test_invalid_tool_or_query_never_reaches_toolgate(gateway, identity):
    client, headers, seen, _ = gateway
    path = f"/api/owner/mcp/tools/{identity}/class"
    assert client.post(path, json=ACT_OUTWARD, headers=headers).status_code == 422
    assert (
        client.post(PATH + "?tool=mcp.other", json=ACT_OUTWARD, headers=headers).status_code == 422
    )
    verify = client.post(
        "/auth/verify",
        headers=headers,
        json={"password": PASSWORD, "operation": {"method": "POST", "path": path, "body": OBSERVE}},
    )
    assert verify.status_code == 422
    assert client.get(PATH).status_code in {404, 405}
    # One class checked, another sent: a repeated key is refused, never read as the last one.
    duplicate = '{"read_class":"act_outward","read_class":"observe","expected_version":3}'
    sent = {**headers, "Content-Type": "application/json"}
    assert client.post(PATH, content=duplicate, headers=sent).status_code == 422
    assert not seen


@pytest.mark.parametrize(
    "status,detail,code",
    [
        (422, {"code": "act_outward_name", "message": SECRET}, "act_outward_name"),
        (422, {"code": "act_outward_hint", "message": SECRET}, "act_outward_hint"),
        (409, {"code": "schema_changed", "message": SECRET}, "schema_changed"),
        (422, {"code": "unsupported_schema", "message": SECRET}, "unsupported_schema"),
        (409, {"code": "definition_conflict", "message": SECRET}, "definition_conflict"),
        (
            409,
            {"code": "DEFINITION_CONFLICT", "message": SECRET, "current_version": 9},
            "definition_conflict",
        ),
        (422, {"code": "invalid_request", "message": SECRET}, "refused"),
        (422, [{"loc": ["body"], "msg": SECRET}], "refused"),
        (409, SECRET, "refused"),
    ],
)
def test_toolgate_refusals_come_back_with_a_plain_message(gateway, status, detail, code):
    client, headers, seen, reply = gateway
    reply.update(status=status, json={"detail": detail})
    response = client.post(PATH, json=OBSERVE, headers=proof(client, headers, OBSERVE))
    assert response.status_code == status
    refusal = response.json()["detail"]
    assert set(refusal) == {"code", "message"} and refusal["code"] == code
    assert SECRET not in response.text and "x-private" not in response.headers
    assert len(seen) == 1


def test_toolgate_down_is_not_retried_and_the_proof_is_spent(gateway):
    client, headers, seen, reply = gateway
    reply["status"] = "timeout"
    checked = proof(client, headers, OBSERVE)
    response = client.post(PATH, json=OBSERVE, headers=checked)
    assert response.status_code == 503 and SECRET not in response.text
    assert client.post(PATH, json=OBSERVE, headers=checked).status_code == 428
    assert len(seen) == 1
