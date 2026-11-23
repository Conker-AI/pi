from __future__ import annotations

import io
import json
from datetime import UTC, datetime

import httpx
import pytest

from gateway.setup_control import (
    MAX_INPUT_BYTES,
    SetupControlError,
    protection_policy,
    record_external_receipt,
    review_boundaries,
    set_protection_policy,
    set_setup_choice,
)
from tests.gateway_setup_helpers import config, receipt


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
        saved = review_boundaries(config(), digest, client, now=datetime(2026, 9, 27, tzinfo=UTC))

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
            set_protection_policy(
                config(), io.BytesIO(b'{"destination":"/mnt/x","extra":1}'), client
            )


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
            record_external_receipt(config(), "protection", io.BytesIO(b'{"extra":true}'), client)


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
