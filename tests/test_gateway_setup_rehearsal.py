"""Gateway setup-control: the rehearsal evidence routes and the CLI that resumes the approval."""

from __future__ import annotations

import json
import sys

import httpx

from gateway.setup_control import (
    finalize_rehearsal,
    rehearsal_status,
    resume_rehearsal_approval,
    review_rehearsal_memory,
    start_rehearsal_approval,
)
from tests.gateway_setup_helpers import config, receipt


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
