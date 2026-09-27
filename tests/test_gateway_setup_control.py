from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime

import httpx
import pytest

from gateway.api import Config
from gateway.setup_control import (
    MAX_INPUT_BYTES,
    SetupControlError,
    activate_setup_model,
    finalize_rehearsal,
    model_options,
    probe_setup_model,
    protection_policy,
    record_external_receipt,
    rehearsal_status,
    resume_rehearsal_approval,
    review_boundaries,
    review_rehearsal_memory,
    set_protection_policy,
    set_setup_choice,
    set_setup_model,
    start_rehearsal_approval,
)


def config() -> Config:
    return Config(
        "https://conker.test",
        "/auth/auth.db",
        "http://pi.test",
        "runtime-" + "r" * 32,
        pi_owner_key="control-" + "c" * 32,
    )


def receipt(step, revision, digest, receipt_id="setup-proof"):
    return {
        "step": step,
        "revision": revision,
        "receiptId": receipt_id,
        "source": "conker.setup",
        "subject": "proof",
        "evidenceDigest": digest,
        "completedAt": "2026-09-27T00:00:00Z",
        "expiresAt": "2026-10-01T00:00:00Z",
        "recordedAt": "2026-09-27T00:00:01Z",
        "state": "valid",
    }


def test_external_receipt_uses_current_revision_and_owner_only():
    digest = "a" * 64
    payload = {
        "receiptId": "setup-proof",
        "source": "conker.setup",
        "subject": "proof",
        "evidenceDigest": digest,
        "completedAt": "2026-09-27T00:00:00Z",
        "expiresAt": "2026-10-01T00:00:00Z",
    }
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=receipt("protection", 3, "b" * 64, "old-proof"))
        assert json.loads(request.content)["expectedRevision"] == 3
        return httpx.Response(200, json=receipt("protection", 4, digest))

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        saved = record_external_receipt(
            config(), "protection", io.BytesIO(json.dumps(payload).encode()), client
        )

    assert saved["revision"] == 4
    assert [request.url.path for request in seen] == [
        "/setup/receipts/protection",
        "/setup/receipts/protection",
    ]
    assert all(request.headers["x-pi-owner-key"] == config().pi_owner_key for request in seen)
    assert all("x-pi-gateway-key" not in request.headers for request in seen)


def test_boundary_review_binds_receipt_to_fresh_policy_digest():
    digest = "c" * 64
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        if request.url.path == "/setup/boundaries":
            return httpx.Response(200, json={"digest": digest, "tools": []})
        if request.method == "GET":
            return httpx.Response(404, json={"detail": "missing"})
        body = json.loads(request.content)
        assert body["expectedRevision"] == 0
        assert body["source"] == "conker.cli"
        assert body["subject"] == "toolgate.policy"
        return httpx.Response(
            200,
            json=receipt("boundaries", 1, digest, body["receiptId"])
            | {
                "source": body["source"],
                "subject": body["subject"],
                "completedAt": body["completedAt"],
                "expiresAt": body["expiresAt"],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        saved = review_boundaries(
            config(), digest, client, now=datetime(2026, 9, 27, tzinfo=UTC)
        )

    assert saved["step"] == "boundaries"
    assert [request.method for request in seen] == ["GET", "GET", "POST"]


def test_setup_choice_uses_current_revision_and_exact_owner_route():
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "step": "memory",
                    "revision": 2,
                    "requestId": "earlier-choice",
                    "choice": "skip",
                    "recordedAt": "2026-09-27T00:00:00Z",
                },
            )
        body = json.loads(request.content)
        assert body["expectedRevision"] == 2 and body["choice"] == "include"
        return httpx.Response(
            200,
            json={
                "step": "memory",
                "revision": 3,
                "requestId": body["requestId"],
                "choice": "include",
                "recordedAt": "2026-09-27T00:00:01Z",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        saved = set_setup_choice(config(), "memory", "include", client)

    assert saved["revision"] == 3 and saved["choice"] == "include"
    assert [request.url.path for request in seen] == [
        "/setup/choices/memory",
        "/setup/choices/memory",
    ]
    assert all(request.headers["x-pi-owner-key"] == config().pi_owner_key for request in seen)


def test_protection_policy_uses_current_revision_and_strict_input():
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "schemaVersion": 1,
                    "revision": 2,
                    "requestId": "earlier-policy",
                    "destinationKind": "mounted_off_machine",
                    "destination": "/mnt/old",
                    "retentionCopies": 5,
                    "policyDigest": "a" * 64,
                    "recordedAt": "2026-09-27T00:00:00Z",
                },
            )
        body = json.loads(request.content)
        assert body["expectedRevision"] == 2
        return httpx.Response(
            200,
            json={
                "schemaVersion": 1,
                "revision": 3,
                "requestId": body["requestId"],
                "destinationKind": "mounted_off_machine",
                "destination": body["destination"],
                "retentionCopies": body["retentionCopies"],
                "policyDigest": "b" * 64,
                "recordedAt": "2026-09-27T00:00:01Z",
            },
        )

    raw = json.dumps({"destination": "/media/conker", "retentionCopies": 7}).encode()
    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        assert protection_policy(config(), client)["revision"] == 2
        saved = set_protection_policy(config(), io.BytesIO(raw), client)

    assert saved["revision"] == 3 and saved["destination"] == "/media/conker"
    assert [request.url.path for request in seen] == [
        "/setup/protection",
        "/setup/protection",
        "/setup/protection",
    ]


def test_protection_policy_rejects_unbounded_or_extra_input_before_write():
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        with pytest.raises(SetupControlError, match="exceeds 16 KiB"):
            set_protection_policy(config(), io.BytesIO(b"x" * (MAX_INPUT_BYTES + 1)), client)
        with pytest.raises(SetupControlError, match="unsupported shape"):
            set_protection_policy(config(), io.BytesIO(b'{"destination":"/mnt/x","extra":1}'), client)


@pytest.mark.parametrize(
    ("step", "choice"),
    [("unknown", "skip"), ("memory", "later"), ("companion", "skip")],
)
def test_setup_choice_refuses_unknown_values_before_network(step, choice):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
        ) as client,
        pytest.raises(SetupControlError, match=r"companion.*memory.*capabilities.*include.*skip"),
    ):
        set_setup_choice(config(), step, choice, client)


def test_setup_model_uses_server_candidates_and_current_revision():
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "revision": 4,
                    "candidates": [
                        {
                            "id": "local-answer",
                            "providerId": "ollama",
                            "providerName": "Local model",
                            "name": "qwen3:4b",
                            "route": "qwen3:4b",
                            "status": "ready",
                            "selected": False,
                            "execution": "local",
                            "dataNotice": "The setup test stays on this server.",
                            "costNotice": "No provider charge.",
                        }
                    ],
                },
            )
        body = json.loads(request.content)
        assert body == {"candidateId": "local-answer", "expectedRevision": 4}
        return httpx.Response(
            200,
            json={"revision": 5, "configuration": {"defaultModelId": "local-answer"}},
        )

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        assert model_options(config(), client)["revision"] == 4
        saved = set_setup_model(config(), "local-answer", client)

    assert saved["revision"] == 5
    assert [request.url.path for request in seen] == [
        "/setup/models",
        "/setup/models",
        "/setup/models",
    ]
    assert all(request.headers["x-pi-owner-key"] == config().pi_owner_key for request in seen)


def test_setup_model_probe_is_revision_bound_and_owner_only():
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "schemaVersion": 1,
                "requestId": body["requestId"],
                "configurationRevision": body["expectedRevision"],
                "candidateId": body["candidateId"],
                "providerId": "ollama",
                "requestedModel": "qwen3:4b",
                "actualModel": "qwen3:4b",
                "execution": "local",
                "responseDigest": "a" * 64,
                "completedAt": "2026-09-27T00:00:00Z",
                "recordedAt": "2026-09-27T00:00:00Z",
            },
        )

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        saved = probe_setup_model(config(), "local-answer", 5, client)

    assert saved["configurationRevision"] == 5
    assert seen[0].url.path == "/setup/models/probe"
    assert seen[0].headers["x-pi-owner-key"] == config().pi_owner_key


def test_activate_setup_model_uses_one_verified_owner_write():
    seen = []

    def upstream(request: httpx.Request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "revision": 4,
                    "candidates": [{
                        "id": "hosted-answer", "providerId": "openai", "providerName": "OpenAI",
                        "name": "Answer", "route": "answer", "status": "unverified", "selected": False,
                        "execution": "hosted", "dataNotice": "The setup test is sent to OpenAI.",
                        "costNotice": "Provider billing may apply.",
                    }],
                },
            )
        body = json.loads(request.content)
        assert request.url.path == "/setup/models/activate"
        assert body["candidateId"] == "hosted-answer" and body["expectedRevision"] == 4
        return httpx.Response(
            200,
            json={
                "revision": 5,
                "candidateId": "hosted-answer",
                "probe": {
                    "schemaVersion": 1, "requestId": body["requestId"], "configurationRevision": 5,
                    "candidateId": "hosted-answer", "providerId": "openai", "requestedModel": "answer",
                    "actualModel": "answer-2026", "execution": "hosted", "responseDigest": "b" * 64,
                    "completedAt": "2026-09-27T00:00:00Z", "recordedAt": "2026-09-27T00:00:00Z",
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        saved = activate_setup_model(config(), "hosted-answer", client)

    assert saved["revision"] == 5
    assert [request.url.path for request in seen] == ["/setup/models", "/setup/models/activate"]


def test_gateway_cli_dispatches_model_activation(monkeypatch, capsys):
    from gateway import __main__ as cli
    from gateway import setup_control

    calls = []

    class Client:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(sys, "argv", ["gateway", "set-setup-model", "local-answer"])
    monkeypatch.setattr("gateway.api.Config.environment", classmethod(lambda _cls: config()))
    monkeypatch.setattr("gateway.api.Config.validate", lambda _self: None)
    monkeypatch.setattr("httpx.Client", Client)
    monkeypatch.setattr(
        setup_control,
        "activate_setup_model",
        lambda _config, candidate, _client: {
            "revision": 1,
            "candidateId": candidate,
            "probe": {"configurationRevision": 1},
        },
    )

    cli.main()

    assert json.loads(capsys.readouterr().out)["candidateId"] == "local-answer"
    assert calls == [{"trust_env": False, "follow_redirects": False, "timeout": 75}]


def test_setup_model_refuses_unknown_candidate_without_write():
    calls = []

    def upstream(request: httpx.Request):
        calls.append(request)
        return httpx.Response(200, json={"revision": 0, "candidates": []})

    with (
        httpx.Client(transport=httpx.MockTransport(upstream)) as client,
        pytest.raises(SetupControlError, match="candidate ID"),
    ):
        set_setup_model(config(), "invented-model", client)
    assert len(calls) == 1 and calls[0].method == "GET"


def test_boundary_digest_mismatch_stops_before_receipt_lookup_or_write():
    calls = 0

    def upstream(_request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"digest": "d" * 64})

    with (
        httpx.Client(transport=httpx.MockTransport(upstream)) as client,
        pytest.raises(SetupControlError, match="policy changed"),
    ):
        review_boundaries(config(), "e" * 64, client)
    assert calls == 1


@pytest.mark.parametrize("step", ["boundaries", "rehearsal", "unknown"])
def test_external_receipt_refuses_non_external_steps_before_network(step):
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
        ) as client,
        pytest.raises(SetupControlError, match="Only protection"),
    ):
        record_external_receipt(config(), step, io.BytesIO(b"{}"), client)


def test_external_receipt_is_bounded_and_strict():
    with httpx.Client(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network used"))
    ) as client:
        with pytest.raises(SetupControlError, match="exceeds 16 KiB"):
            record_external_receipt(
                config(), "protection", io.BytesIO(b"x" * (MAX_INPUT_BYTES + 1)), client
            )
        with pytest.raises(SetupControlError, match="unsupported shape"):
            record_external_receipt(
                config(), "protection", io.BytesIO(b'{"extra":true}'), client
            )


def test_setup_errors_never_include_upstream_body():
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(403, text="credential detail")
            )
        ) as client,
        pytest.raises(SetupControlError, match="HTTP 403") as failure,
    ):
        review_boundaries(config(), "f" * 64, client)
    assert "credential detail" not in str(failure.value)


def test_rehearsal_control_uses_exact_server_owned_evidence_routes():
    seen = []
    base = {
        "schemaVersion": 1,
        "state": "in_progress",
        "conversation": {"state": "complete", "detail": "Conversation complete."},
        "memoryReview": {"state": "missing", "detail": "Review memory."},
        "approval": {"state": "missing", "detail": "Start approval."},
        "approvalRequestId": None,
        "canFinalize": False,
    }

    def upstream(request: httpx.Request):
        seen.append(request)
        path = request.url.path
        if path == "/setup/choices/memory":
            return httpx.Response(
                200,
                json={
                    "step": "memory",
                    "revision": 2,
                    "requestId": "choice-memory",
                    "choice": "include",
                    "recordedAt": "2026-09-27T00:00:00Z",
                },
            )
        if path == "/setup/rehearsal/finalize":
            return httpx.Response(
                200,
                json={
                    **receipt("rehearsal", 1, "e" * 64, "rehearsal-proof"),
                    "source": "conker.first-run-rehearsal",
                    "subject": "owner.daily-workflow",
                },
            )
        body = json.loads(request.content) if request.content else {}
        return httpx.Response(
            200,
            json={
                **base,
                "approvalRequestId": body.get("requestId"),
            },
        )

    with httpx.Client(transport=httpx.MockTransport(upstream)) as client:
        assert rehearsal_status(config(), client)["conversation"]["state"] == "complete"
        review_rehearsal_memory(config(), client)
        started = start_rehearsal_approval(config(), client)
        request_id = started["approvalRequestId"]
        resume_rehearsal_approval(config(), request_id, client)
        assert finalize_rehearsal(config(), client)["source"] == "conker.first-run-rehearsal"

    assert [request.url.path for request in seen] == [
        "/setup/rehearsal",
        "/setup/choices/memory",
        "/setup/rehearsal/memory-review",
        "/setup/rehearsal/approval/start",
        "/setup/rehearsal/approval/resume",
        "/setup/rehearsal/finalize",
    ]
    review = json.loads(seen[2].content)
    assert review["expectedChoiceRevision"] == 2
    assert set(review) == {"requestId", "expectedChoiceRevision"}


def test_gateway_cli_dispatches_rehearsal_resume(monkeypatch, capsys):
    from gateway import __main__ as cli
    from gateway import setup_control

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        sys,
        "argv",
        ["gateway", "setup-rehearsal", "resume-approval:setup-approval-1"],
    )
    monkeypatch.setattr("gateway.api.Config.environment", classmethod(lambda _cls: config()))
    monkeypatch.setattr("gateway.api.Config.validate", lambda _self: None)
    monkeypatch.setattr("httpx.Client", Client)
    monkeypatch.setattr(
        setup_control,
        "resume_rehearsal_approval",
        lambda _config, request_id, _client: {"approvalRequestId": request_id},
    )

    cli.main()

    assert json.loads(capsys.readouterr().out)["approvalRequestId"] == "setup-approval-1"
