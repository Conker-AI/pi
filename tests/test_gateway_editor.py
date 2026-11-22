"""The gateway editor routes: drafts, publication, catalogue and workflow access."""

import json

import pytest
from test_gateway_api import OWNER, PASSWORD, gateway, sign_in, verified_headers  # noqa: F401


def test_editor_draft_write_needs_only_the_session(gateway):
    client, _, seen = gateway
    path = "/api/owner/editor-drafts/example"
    assert client.get(path).status_code == 401
    headers = sign_in(client)
    body = {"expected_revision": 0, "document": {"id": "example"}}
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, json=body, headers={**headers, "X-CSRF-Token": "x"}).status_code == 403
    assert client.post(path + "?force=1", json=body, headers=headers).status_code == 422
    assert not seen
    assert client.post(path, json=body, headers=headers).status_code == 200
    assert seen[-1].url.path == "/v2/owner/editor-drafts/example"
    assert json.loads(seen[-1].content) == body
    assert seen[-1].headers["X-ToolGate-Owner-Key"] == OWNER
    assert "X-ToolGate-Execution-Key" not in seen[-1].headers
    assert "cookie" not in seen[-1].headers and "x-conker-verification" not in seen[-1].headers
    assert client.post(path, json=body, headers=headers).status_code == 200
    assert len(seen) == 2


def test_editor_draft_routes_do_not_widen_to_execution_or_arbitrary_paths(gateway):
    client, _, seen = gateway
    headers = sign_in(client)
    for suffix in ("?limit=0", "?limit=101", "?limit=2&limit=3", "?secret=x", "?after=../vault"):
        assert client.get("/api/owner/editor-drafts" + suffix).status_code == 422
    assert not seen
    assert client.get("/api/owner/editor-drafts?limit=2&after=a").status_code == 200
    assert str(seen[-1].url).endswith("/v2/owner/editor-drafts?limit=2&after=a")
    for path in ("/api/owner/editor-drafts/example/delete", "/api/owner/editor-drafts/example/run"):
        result = client.post(
            "/auth/verify",
            headers=headers,
            json={"password": PASSWORD, "operation": {"method": "POST", "path": path, "body": {}}},
        )
        assert result.status_code == 422


def publication_body(**changes):
    return {
        "expected_revision": 2,
        "expected_publication_version": 1,
        "authorization": "owner_confirmation",
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"authorization": "auto"},
        {"conkerMayUse": True},
        {"authorization": "auto", "conkerMayUse": True},
    ],
    ids=["auto", "conker-may-use", "both"],
)
def test_editor_publication_that_widens_requires_exact_one_use_proof(gateway, changes):
    client, _, seen = gateway
    path = "/api/owner/editor-drafts/example/publish"
    headers = sign_in(client)
    body = publication_body(**changes)
    assert client.post(path, json=body, headers=headers).status_code == 428
    verified = verified_headers(client, headers, path, body)
    widened = {**body, "authorization": "auto", "conkerMayUse": True, "expected_revision": 3}
    assert client.post(path, json=widened, headers=verified).status_code == 428
    assert not seen
    assert client.post(path, json=body, headers=verified).status_code == 200
    assert seen[-1].url.path == "/v2/owner/editor-drafts/example/publish"
    assert json.loads(seen[-1].content) == body
    assert seen[-1].headers["X-ToolGate-Owner-Key"] == OWNER
    # Only letting Conker use it names the chat's own scoped key.
    assert ("X-ToolGate-Execution-Key" in seen[-1].headers) == bool(body.get("conkerMayUse"))
    assert client.post(path, json=body, headers=verified).status_code == 428
    assert len(seen) == 1


@pytest.mark.parametrize("extra", [{}, {"conkerMayUse": False}], ids=["plain", "may-use-false"])
def test_editor_publication_to_ask_each_time_needs_only_the_session(gateway, extra):
    client, _, seen = gateway
    path = "/api/owner/editor-drafts/example/publish"
    headers = sign_in(client)
    body = publication_body(**extra)
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, json=body, headers=headers).status_code == 200
    assert seen[-1].url.path == "/v2/owner/editor-drafts/example/publish"
    assert json.loads(seen[-1].content) == body
    assert seen[-1].headers["X-ToolGate-Owner-Key"] == OWNER
    assert "X-ToolGate-Execution-Key" not in seen[-1].headers
    assert "x-conker-verification" not in seen[-1].headers
    for operation in ("publications", "validation"):
        assert client.get(f"/api/owner/editor-drafts/example/{operation}").status_code == 200
        assert (
            client.get(f"/api/owner/editor-drafts/example/{operation}?secret=x").status_code == 422
        )


def test_editor_catalogue_is_a_narrow_read_only_owner_route(gateway):
    client, _, seen = gateway
    path = "/api/owner/editor-capabilities"
    assert client.get(path).status_code == 401
    sign_in(client)
    for suffix in ("?kind=secret", "?limit=0", "?q=a&q=b", "?after=../secret", "?extra=x"):
        assert client.get(path + suffix).status_code == 422
    assert seen == []
    assert client.get(path + "?kind=tool&q=echo&limit=10").status_code == 200
    assert seen[-1].url.path == "/v2/owner/editor-capabilities"
    assert seen[-1].headers["X-ToolGate-Owner-Key"] == OWNER
    assert client.post(path, json={}).status_code == 405


def execution_body(**fields):
    return {"version": 1, "digest": "b" * 64, **fields}


def test_editor_workflow_access_on_requires_proof_and_both_host_credentials(gateway):
    client, _, seen = gateway
    headers = sign_in(client)
    path = "/api/owner/editor-drafts/example/access"
    body = execution_body(enabled=True)
    assert client.post(path, headers=headers, json=body).status_code == 428
    verified = verified_headers(client, headers, path, body)
    assert client.post(path, headers=verified, json={**body, "version": 2}).status_code == 428
    assert not seen
    assert client.post(path, headers=verified, json=body).status_code == 200
    assert seen[-1].headers["X-ToolGate-Owner-Key"] == OWNER
    assert seen[-1].headers["X-ToolGate-Execution-Key"] == "tgx_" + "e" * 40
    assert client.post(path, headers=verified, json=body).status_code == 428


@pytest.mark.parametrize(
    "operation,fields",
    [
        ("access", {"enabled": False}),
        ("runs", {"action_id": "editor_" + "a" * 32, "args": {}}),
    ],
)
def test_editor_workflow_withdrawal_and_runs_need_only_the_session(gateway, operation, fields):
    client, _, seen = gateway
    headers = sign_in(client)
    path = f"/api/owner/editor-drafts/example/{operation}"
    body = execution_body(**fields)
    assert client.post(path, json=body).status_code == 403
    assert not seen
    assert client.post(path, headers=headers, json=body).status_code == 200
    assert json.loads(seen[-1].content) == body
    assert seen[-1].headers["X-ToolGate-Owner-Key"] == OWNER
    assert seen[-1].headers["X-ToolGate-Execution-Key"] == "tgx_" + "e" * 40
    assert "x-conker-verification" not in seen[-1].headers
    assert client.post(path, headers=headers, json=body).status_code == 200
