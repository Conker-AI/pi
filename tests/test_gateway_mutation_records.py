"""Gateway writes for memory, proposals, tasks, artifacts and chat settings."""

from __future__ import annotations

import io
import json

import httpx
import pytest

from gateway.mutation import MutationError, apply_resource
from pi.api.browser_contract import owner_allowed, runtime_allowed
from tests.test_gateway_mutation import config


def test_memory_forget_apply_preserves_review_revision_and_request_identity():
    payload = {
        "operation": "forget",
        "request_id": "forget_1234567890abcdef",
        "memory_id": "memory.important:1",
        "expected_revision": 8,
    }
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "requestId": payload["request_id"],
                "memoryId": payload["memory_id"],
                "status": "forgotten",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "memory", io.BytesIO(json.dumps(payload).encode()), client
        )

    assert result["status"] == "forgotten"
    assert seen[0].url.path == "/memory/forget"
    assert owner_allowed("POST", seen[0].url.path)
    assert json.loads(seen[0].content) == {
        "request_id": payload["request_id"],
        "memory_id": payload["memory_id"],
        "expected_revision": 8,
    }


def test_memory_apply_rejects_generic_delete_and_missing_review_revision():
    invalid = [
        {"operation": "delete", "memory_id": "memory.important:1"},
        {
            "operation": "forget",
            "request_id": "forget_1234567890abcdef",
            "memory_id": "memory.important:1",
        },
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(config(), "memory", io.BytesIO(json.dumps(payload).encode()), client)


@pytest.mark.parametrize("decision", ["accept", "decline", "never"])
def test_proposal_decision_uses_runtime_channel_and_grants_no_action(decision):
    payload = {"operation": "decide", "id": "proposal_123", "decision": decision}
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"id": payload["id"], "state": decision})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "proposals", io.BytesIO(json.dumps(payload).encode()), client
        )

    assert result["id"] == payload["id"]
    request = seen[0]
    assert request.url.path == "/proposals/proposal_123/decision"
    assert runtime_allowed("POST", request.url.path)
    assert json.loads(request.content) == {"decision": decision}
    assert request.headers["x-pi-gateway-key"] == config().pi_key
    assert "x-pi-owner-key" not in request.headers


def test_proposal_apply_rejects_execution_and_unknown_decisions_before_network():
    invalid = [
        {"operation": "execute", "id": "proposal_123"},
        {"operation": "decide", "id": "proposal_123", "decision": "run"},
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(
                    config(), "proposals", io.BytesIO(json.dumps(payload).encode()), client
                )


@pytest.mark.parametrize(
    ("payload", "path", "forwarded"),
    [
        (
            {
                "operation": "create",
                "request_id": "task_1234567890abcdef",
                "session_id": "ses_1234567890abcdef",
                "outcome": "Prepare the release.",
                "criteria": ["Tests pass"],
                "parent_task_id": None,
                "run_ids": [],
            },
            "/tasks",
            {
                "request_id": "task_1234567890abcdef",
                "session_id": "ses_1234567890abcdef",
                "outcome": "Prepare the release.",
                "criteria": ["Tests pass"],
                "parent_task_id": None,
                "run_ids": [],
            },
        ),
        (
            {
                "operation": "update",
                "id": "tsk_" + "1" * 32,
                "expected_revision": 2,
                "outcome": "Prepare the release safely.",
                "criteria": ["Tests pass", "Evidence retained"],
                "parent_task_id": None,
                "run_ids": [],
            },
            "/tasks/tsk_" + "1" * 32 + "/update",
            {
                "expected_revision": 2,
                "outcome": "Prepare the release safely.",
                "criteria": ["Tests pass", "Evidence retained"],
                "parent_task_id": None,
                "run_ids": [],
            },
        ),
        (
            {
                "operation": "transition",
                "id": "tsk_" + "1" * 32,
                "expected_revision": 3,
                "status": "completed",
                "note": "Acceptance passed.",
                "completed_criterion_ids": ["criterion_1"],
            },
            "/tasks/tsk_" + "1" * 32 + "/transition",
            {
                "expected_revision": 3,
                "status": "completed",
                "note": "Acceptance passed.",
                "completed_criterion_ids": ["criterion_1"],
            },
        ),
        (
            {
                "operation": "archive",
                "id": "tsk_" + "1" * 32,
                "expected_revision": 4,
                "archived": True,
            },
            "/tasks/tsk_" + "1" * 32 + "/archive",
            {"expected_revision": 4, "archived": True},
        ),
    ],
)
def test_task_apply_uses_typed_runtime_operations(payload, path, forwarded):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"id": "tsk_" + "1" * 32, "revision": 5})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(config(), "tasks", io.BytesIO(json.dumps(payload).encode()), client)

    assert result["revision"] == 5
    request = seen[0]
    assert request.url.path == path
    assert runtime_allowed("POST", path)
    assert json.loads(request.content) == forwarded
    assert request.headers["x-pi-gateway-key"] == config().pi_key
    assert "x-pi-owner-key" not in request.headers


def test_task_apply_rejects_invalid_transition_and_unstable_request_before_network():
    invalid = [
        {
            "operation": "transition",
            "id": "tsk_" + "1" * 32,
            "expected_revision": 1,
            "status": "deleted",
            "note": "No.",
        },
        {
            "operation": "create",
            "request_id": "short",
            "session_id": "session",
            "outcome": "Outcome",
            "criteria": ["Done"],
        },
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(config(), "tasks", io.BytesIO(json.dumps(payload).encode()), client)


@pytest.mark.parametrize(
    ("payload", "path", "forwarded"),
    [
        (
            {
                "operation": "create",
                "title": "Release notes",
                "content": {"kind": "markdown", "text": "Ready."},
                "taskId": None,
            },
            "/artifacts",
            {
                "title": "Release notes",
                "content": {"kind": "markdown", "text": "Ready."},
            },
        ),
        (
            {
                "operation": "from_message",
                "title": "Answer",
                "sessionId": "ses_" + "2" * 16,
                "messageId": "msg_" + "3" * 16,
                "taskId": None,
            },
            "/artifacts/from-message",
            {
                "title": "Answer",
                "sessionId": "ses_" + "2" * 16,
                "messageId": "msg_" + "3" * 16,
            },
        ),
        (
            {
                "operation": "append",
                "id": "artifact_" + "4" * 32,
                "expected_revision": 2,
                "content": {"kind": "code", "text": "print('ok')", "language": "python"},
                "title": None,
                "note": "Verified",
                "preserveCitations": True,
            },
            "/artifacts/artifact_" + "4" * 32 + "/versions",
            {
                "expected_revision": 2,
                "content": {"kind": "code", "text": "print('ok')", "language": "python"},
                "note": "Verified",
                "preserveCitations": True,
            },
        ),
        (
            {
                "operation": "restore",
                "id": "artifact_" + "4" * 32,
                "expected_revision": 3,
                "version": 1,
            },
            "/artifacts/artifact_" + "4" * 32 + "/restore",
            {"expected_revision": 3, "version": 1},
        ),
        (
            {
                "operation": "archive",
                "id": "artifact_" + "4" * 32,
                "expected_revision": 4,
                "archived": True,
            },
            "/artifacts/artifact_" + "4" * 32 + "/archive",
            {"expected_revision": 4, "archived": True},
        ),
    ],
)
def test_artifact_apply_preserves_inert_versioned_operations(payload, path, forwarded):
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"schemaVersion": 1, "revision": 5})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "artifacts", io.BytesIO(json.dumps(payload).encode()), client
        )

    assert result["revision"] == 5
    request = seen[0]
    assert request.url.path == path
    assert owner_allowed("POST", path)
    assert json.loads(request.content) == forwarded


def test_artifact_apply_rejects_execution_download_and_unsafe_media_before_network():
    invalid = [
        {"operation": "execute", "id": "artifact_" + "4" * 32},
        {"operation": "download", "id": "artifact_" + "4" * 32},
        {
            "operation": "create",
            "title": "Unsafe",
            "content": {
                "kind": "media",
                "mediaType": "image",
                "url": "file:///etc/passwd",
                "description": "No",
            },
        },
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(
                    config(), "artifacts", io.BytesIO(json.dumps(payload).encode()), client
                )


def test_session_apply_uses_revision_checked_owner_settings_operation():
    payload = {
        "operation": "update",
        "id": "ses_1234567890abcdef",
        "expected_revision": 3,
        "settings": {
            "agentId": "companion",
            "privacy": {"memoryDisabled": True, "harnessDisabled": True},
            "projectId": None,
            "projectSources": [],
            "presentationMode": "focus",
            "access": "default",
        },
    }
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"revision": 4, "settings": payload["settings"]})

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        result = apply_resource(
            config(), "sessions", io.BytesIO(json.dumps(payload).encode()), client
        )

    assert result["revision"] == 4
    request = seen[0]
    assert request.url.path == "/sessions/ses_1234567890abcdef/settings"
    assert owner_allowed("POST", request.url.path)
    assert json.loads(request.content) == {
        "expected_revision": 3,
        "settings": payload["settings"],
    }
    assert request.headers["x-pi-owner-key"] == config().pi_owner_key


def test_session_apply_rejects_missing_revision_invalid_sources_and_authority():
    base = {
        "operation": "update",
        "id": "ses_1234567890abcdef",
        "expected_revision": 3,
        "settings": {
            "agentId": "companion",
            "privacy": {"memoryDisabled": False, "harnessDisabled": False},
            "projectId": None,
            "projectSources": [],
            "presentationMode": None,
        },
    }
    invalid = [
        {key: value for key, value in base.items() if key != "expected_revision"},
        base
        | {
            "settings": base["settings"]
            | {"projectSources": [{"kind": "conversation", "sessionId": "ses_1234567890abcdef"}]}
        },
        base | {"authority": "admin"},
    ]
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        for payload in invalid:
            with pytest.raises(MutationError, match="unsupported shape"):
                apply_resource(
                    config(), "sessions", io.BytesIO(json.dumps(payload).encode()), client
                )
