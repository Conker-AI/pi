import hashlib
import time
from contextlib import closing
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from pi import api, setup_choices, setup_receipts, setup_rehearsal, setup_status
from pi.browser_contract import owner_allowed, runtime_allowed
from pi.store import Store
from pi.toolgate import ApprovalRequired, ToolPending, ToolResult

NOW = datetime(2026, 9, 27, 8, 0, tzinfo=UTC)
NOW_TS = NOW.timestamp()
OWNER = "setup-rehearsal-owner-" + "o" * 32


class Memory:
    client = object()

    def health(self):
        return {"status": "ok"}


class Gate:
    def __init__(self):
        self.calls = []

    def invoke(self, tool_id, args, approval_request_id=None, *, action_id, job_id=None):
        self.calls.append((tool_id, args, approval_request_id, action_id))
        if approval_request_id is None:
            return ApprovalRequired(
                "approval-setup-1",
                "2026-09-27T08:10:00Z",
                "Owner confirmation required.",
                tool_id,
                args,
            )
        return ToolResult(True, {"digest": "content-free"}, tool_id)


def choose_memory(store, choice="include"):
    return setup_choices.record(
        store,
        "memory",
        setup_choices.ChoiceInput(
            requestId="setup-memory-choice-1", choice=choice, expectedRevision=0
        ),
        now=NOW,
    )


def complete_conversation(store, *, ended_at=NOW_TS - 30):
    session = store.create_session("First conversation")
    turn = store.start_turn(session)
    store.complete_turn(turn, "Ready.")
    with store._connect() as db:
        db.execute("UPDATE turns SET ended_at=? WHERE id=?", (ended_at, turn))
    return turn


def test_rehearsal_requires_real_conversation_memory_review_and_approval(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        choice = choose_memory(store)
        gate = Gate()
        assert setup_rehearsal.status(store, now=NOW).state == "in_progress"

        complete_conversation(store)
        reviewed = setup_rehearsal.review_memory(
            store,
            setup_rehearsal.MemoryReviewInput(
                requestId="memory-review-1", expectedChoiceRevision=choice.revision
            ),
            Memory(),
            now=NOW_TS - 20,
        )
        assert reviewed.conversation.state == "complete"
        assert reviewed.memoryReview.state == "complete"

        waiting = setup_rehearsal.start_approval(
            store,
            setup_rehearsal.ApprovalInput(requestId="approval-flow-1"),
            gate,
            now=NOW_TS - 10,
        )
        assert waiting.approval.state == "awaiting_owner"
        ready = setup_rehearsal.resume_approval(
            store,
            setup_rehearsal.ApprovalInput(requestId="approval-flow-1"),
            gate,
            now=NOW_TS - 5,
        )
        assert ready.state == "ready" and ready.canFinalize is True

        receipt = setup_rehearsal.finalize(store, now=NOW)
        assert receipt.source == "conker.first-run-rehearsal"
        assert receipt.subject == "owner.daily-workflow"
        assert setup_rehearsal.status(store, now=NOW).state == "complete"
        assert setup_rehearsal.finalize(store, now=NOW) == receipt
        assert [call[2] for call in gate.calls] == [None, "approval-setup-1"]

        setup_choices.record(
            store,
            "memory",
            setup_choices.ChoiceInput(
                requestId="setup-memory-choice-2",
                choice="skip",
                expectedRevision=choice.revision,
            ),
            now=NOW,
        )
        changed = setup_rehearsal.status(store, now=NOW)
        assert changed.state == "in_progress"
        assert changed.memoryReview.state == "missing"
        assert setup_status._receipt_step(store, "rehearsal", now=NOW).state == (
            "degraded"
        )


def test_memory_review_is_revision_bound_idempotent_and_content_free(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        choice = choose_memory(store, "skip")
        assert "off for new conversations" in setup_rehearsal.status(
            store, now=NOW
        ).memoryReview.detail
        body = setup_rehearsal.MemoryReviewInput(
            requestId="memory-review-1", expectedChoiceRevision=choice.revision
        )
        first = setup_rehearsal.review_memory(store, body, Memory(), now=NOW_TS)
        second = setup_rehearsal.review_memory(store, body, Memory(), now=NOW_TS + 1)
        assert first == second
        with store._connect() as db:
            row = db.execute("SELECT * FROM setup_rehearsal_memory_reviews").fetchone()
        assert row["choice"] == "skip" and row["service_state"] == "off"
        assert "content" not in dict(row)

        setup_choices.record(
            store,
            "memory",
            setup_choices.ChoiceInput(
                requestId="setup-memory-choice-2",
                choice="include",
                expectedRevision=choice.revision,
            ),
            now=NOW,
        )
        with pytest.raises(setup_rehearsal.RehearsalError) as stale:
            setup_rehearsal.review_memory(store, body, Memory(), now=NOW_TS + 2)
        assert stale.value.code == "revision_conflict"


def test_approval_start_replay_never_dispatches_twice_and_unknown_never_retries(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        gate = Gate()
        body = setup_rehearsal.ApprovalInput(requestId="approval-flow-1")
        setup_rehearsal.start_approval(store, body, gate, now=NOW_TS)
        setup_rehearsal.start_approval(store, body, gate, now=NOW_TS + 1)
        assert len(gate.calls) == 1

    class UnknownGate(Gate):
        def invoke(self, tool_id, args, approval_request_id=None, *, action_id, job_id=None):
            self.calls.append((tool_id, args, approval_request_id, action_id))
            return ToolPending("outcome_unknown", "do not retry", action_id)

    with closing(Store(tmp_path / "unknown.db")) as store:
        gate = UnknownGate()
        body = setup_rehearsal.ApprovalInput(requestId="approval-flow-unknown")
        result = setup_rehearsal.start_approval(store, body, gate, now=NOW_TS)
        assert result.approval.state == "outcome_unknown"
        setup_rehearsal.start_approval(store, body, gate, now=NOW_TS + 1)
        assert len(gate.calls) == 1


def test_finalize_refuses_claims_without_all_three_proofs(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        with pytest.raises(setup_rehearsal.RehearsalError) as error:
            setup_rehearsal.finalize(store, now=NOW)
        assert error.value.code == "rehearsal_incomplete"
        assert setup_receipts.current(store, "rehearsal", now=NOW) is None


def test_owner_api_runs_exact_rehearsal_routes_and_rejects_uploaded_receipts(
    tmp_path, monkeypatch
):
    store = Store(tmp_path / "pi.db")
    choice = choose_memory(store)
    complete_conversation(store, ended_at=time.time() - 30)
    gate = Gate()
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "memory", Memory(), raising=False)
    monkeypatch.setattr(api.app.state, "toolgate", gate, raising=False)
    monkeypatch.setattr(
        api.app.state,
        "owner_key_hash",
        hashlib.sha256(OWNER.encode()).hexdigest(),
        raising=False,
    )
    client = TestClient(api.app)
    headers = {"X-Pi-Owner-Key": OWNER}
    try:
        assert client.get("/setup/rehearsal").status_code == 401
        assert client.get("/setup/rehearsal", headers=headers).json()["state"] == "in_progress"
        reviewed = client.post(
            "/setup/rehearsal/memory-review",
            headers=headers,
            json={"requestId": "api-memory-review-1", "expectedChoiceRevision": choice.revision},
        )
        assert reviewed.status_code == 200
        waiting = client.post(
            "/setup/rehearsal/approval/start",
            headers=headers,
            json={"requestId": "api-approval-flow-1"},
        )
        assert waiting.json()["approval"]["state"] == "awaiting_owner"
        resumed = client.post(
            "/setup/rehearsal/approval/resume",
            headers=headers,
            json={"requestId": "api-approval-flow-1"},
        )
        assert resumed.json()["canFinalize"] is True
        saved = client.post("/setup/rehearsal/finalize", headers=headers)
        assert saved.status_code == 200
        assert saved.json()["source"] == "conker.first-run-rehearsal"

        uploaded = setup_receipts.ReceiptInput(
            receiptId="uploaded-rehearsal",
            source="untrusted.client",
            subject="owner.daily-workflow",
            evidenceDigest="f" * 64,
            completedAt=datetime.now(UTC),
            expiresAt=datetime.now(UTC).replace(year=2027),
            expectedRevision=1,
        )
        rejected = client.post(
            "/setup/receipts/rehearsal",
            headers=headers,
            json=uploaded.model_dump(mode="json"),
        )
        assert rejected.status_code == 403
        assert setup_receipts.current(store, "rehearsal").source == (
            "conker.first-run-rehearsal"
        )
    finally:
        store.close()

    assert owner_allowed("GET", "/setup/rehearsal")
    for path in (
        "/setup/rehearsal/memory-review",
        "/setup/rehearsal/approval/start",
        "/setup/rehearsal/approval/resume",
        "/setup/rehearsal/finalize",
    ):
        assert owner_allowed("POST", path)
        assert not runtime_allowed("POST", path)
    assert not owner_allowed("POST", "/setup/receipts/rehearsal")
