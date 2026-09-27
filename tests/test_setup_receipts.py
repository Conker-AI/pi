import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_setup_status import (
    Adapter,
    Memory,
    Router,
    ToolGate,
    choose_optional,
    configuration,
    verify_model,
)

from pi import (
    api,
    model_roles,
    setup_choices,
    setup_protection,
    setup_receipts,
    setup_rehearsal,
    setup_status,
)
from pi.browser_contract import owner_allowed, runtime_allowed
from pi.store import Store
from pi.toolgate import ApprovalRequired, ToolResult

OWNER = "receipt-owner-key-" + "o" * 32
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
DEFAULT_EXPIRY = object()


class RehearsalGate:
    def invoke(self, tool_id, args, approval_request_id=None, *, action_id, job_id=None):
        if approval_request_id is None:
            return ApprovalRequired(
                "approval-receipts-1",
                "2026-09-26T12:10:00Z",
                "Owner confirmation required.",
                tool_id,
                args,
            )
        return ToolResult(True, {"digest": "content-free"}, tool_id)


def receipt(
    identity: str,
    *,
    digest: str = "a" * 64,
    source: str = "conker.host-verifier",
    subject: str = "installation.primary",
    completed: datetime = NOW,
    expires: datetime | object | None = DEFAULT_EXPIRY,
    expected: int = 0,
) -> setup_receipts.ReceiptInput:
    expiry = completed + timedelta(days=30) if expires is DEFAULT_EXPIRY else expires
    return setup_receipts.ReceiptInput(
        receiptId=identity,
        source=source,
        subject=subject,
        evidenceDigest=digest,
        completedAt=completed,
        expiresAt=expiry,
        expectedRevision=expected,
    )


def test_receipts_survive_restart_increment_and_replay_idempotently(tmp_path):
    path = tmp_path / "pi.db"
    first_input = receipt("receipt-boundaries-01")
    with closing(Store(path)) as store:
        first = setup_receipts.record(store, "boundaries", first_input, now=NOW)
        replay = setup_receipts.record(store, "boundaries", first_input, now=NOW)
        assert first == replay
        assert first.revision == 1

    with closing(Store(path)) as store:
        loaded = setup_receipts.current(store, "boundaries", now=NOW)
        second = setup_receipts.record(
            store,
            "boundaries",
            receipt("receipt-boundaries-02", digest="b" * 64, expected=1),
            now=NOW,
        )
    assert loaded == first
    assert second.revision == 2


def test_conflicting_replays_revisions_and_cross_step_evidence_fail_closed(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_receipts.record(store, "boundaries", receipt("receipt-shared"), now=NOW)
        with pytest.raises(setup_receipts.ReceiptError, match="different evidence") as conflict:
            setup_receipts.record(
                store,
                "boundaries",
                receipt("receipt-shared", digest="b" * 64),
                now=NOW,
            )
        assert conflict.value.code == "replay_conflict"

        with pytest.raises(setup_receipts.ReceiptError) as revision:
            setup_receipts.record(
                store,
                "boundaries",
                receipt("receipt-late", digest="c" * 64, expected=0),
                now=NOW,
            )
        assert revision.value.code == "revision_conflict"

        with pytest.raises(setup_receipts.ReceiptError) as crossed:
            setup_receipts.record(
                store,
                "protection",
                receipt(
                    "receipt-protection",
                    digest="a" * 64,
                    expires=NOW + timedelta(days=10),
                ),
                now=NOW,
            )
        assert crossed.value.code == "cross_step_evidence"


@pytest.mark.parametrize(
    ("step", "value", "code"),
    [
        (
            "boundaries",
            receipt("receipt-future", completed=NOW + timedelta(minutes=6)),
            "future_completion",
        ),
        (
            "boundaries",
            receipt("receipt-boundary-no-expiry", digest="9" * 64, expires=None),
            "expiry_required",
        ),
        (
            "protection",
            receipt("receipt-no-expiry", digest="b" * 64, expires=None),
            "expiry_required",
        ),
        (
            "rehearsal",
            receipt(
                "receipt-expired",
                digest="c" * 64,
                completed=NOW - timedelta(days=2),
                expires=NOW - timedelta(days=1),
            ),
            "expired_evidence",
        ),
        (
            "rehearsal",
            receipt(
                "receipt-too-long",
                digest="d" * 64,
                expires=NOW + timedelta(days=31),
            ),
            "validity_too_long",
        ),
    ],
)
def test_temporal_policy_rejects_untrustworthy_evidence(tmp_path, step, value, code):
    with (
        closing(Store(tmp_path / "pi.db")) as store,
        pytest.raises(setup_receipts.ReceiptError) as error,
    ):
        setup_receipts.record(store, step, value, now=NOW)
    assert error.value.code == code


def test_expiry_is_explicit_and_setup_can_reach_complete(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_choices.record(
            store,
            "companion",
            setup_choices.ChoiceInput(
                requestId="setup-companion-complete-accept",
                choice="accept",
                expectedRevision=0,
            ),
            now=NOW,
        )
        model_roles.save(
            store, model_roles.Update(expected_revision=0, configuration=configuration())
        )
        router = Router(Adapter())
        verify_model(store, router, "setup-model-receipts-complete")
        memory_choice = choose_optional(
            store, "memory", "include", "setup-memory-receipts-include"
        )
        choose_optional(
            store, "capabilities", "include", "setup-capabilities-receipts-include"
        )
        setup_receipts.record(
            store, "boundaries", receipt("receipt-boundaries", digest="1" * 64), now=NOW
        )
        protection_policy = setup_protection.save(
            store,
            setup_protection.PolicyInput(
                requestId="setup-protection-receipts-policy",
                destination="/mnt/conker-backups",
                retentionCopies=7,
                expectedRevision=0,
            ),
            now=NOW,
        )
        setup_receipts.record(
            store,
            "protection",
            receipt(
                "receipt-protection",
                digest="2" * 64,
                source="conker.coordinated-backup",
                subject=setup_protection.receipt_subject(protection_policy),
                expires=NOW + timedelta(days=10),
            ),
            now=NOW,
        )
        session = store.create_session("First conversation")
        turn = store.start_turn(session)
        store.complete_turn(turn, "Ready.")
        with store._connect() as db:
            db.execute(
                "UPDATE turns SET ended_at=? WHERE id=?",
                ((NOW - timedelta(seconds=30)).timestamp(), turn),
            )
        setup_rehearsal.review_memory(
            store,
            setup_rehearsal.MemoryReviewInput(
                requestId="receipt-memory-review-1",
                expectedChoiceRevision=memory_choice.revision,
            ),
            Memory(),
            now=(NOW - timedelta(seconds=20)).timestamp(),
        )
        gate = RehearsalGate()
        setup_rehearsal.start_approval(
            store,
            setup_rehearsal.ApprovalInput(requestId="receipt-approval-flow-1"),
            gate,
            now=(NOW - timedelta(seconds=10)).timestamp(),
        )
        setup_rehearsal.resume_approval(
            store,
            setup_rehearsal.ApprovalInput(requestId="receipt-approval-flow-1"),
            gate,
            now=(NOW - timedelta(seconds=5)).timestamp(),
        )
        setup_rehearsal.finalize(store, now=NOW)
        complete = setup_status.load(
            store,
            owner_key_configured=True,
            memory=Memory(),
            toolgate=ToolGate(),
            router=router,
            now=NOW,
        )
        stale = setup_status.load(
            store,
            owner_key_configured=True,
            memory=Memory(),
            toolgate=ToolGate(),
            router=router,
            now=NOW + timedelta(days=11),
        )
        boundary_stale = setup_status.load(
            store,
            owner_key_configured=True,
            memory=Memory(),
            toolgate=ToolGate(),
            router=router,
            now=NOW + timedelta(days=31),
        )

    assert complete.state == "complete"
    assert complete.currentStep is None
    assert complete.recommendedNextOperation is None
    assert all(step.state == "complete" for step in complete.steps if step.required)
    protection = next(step for step in stale.steps if step.id == "protection")
    assert protection.state == "degraded"
    assert protection.blockingReasonCode == "protection_receipt_stale"
    assert protection.evidence[0].status == "degraded"
    assert stale.currentStep == "protection"
    boundary = next(step for step in boundary_stale.steps if step.id == "boundaries")
    assert boundary.state == "degraded"
    assert boundary.blockingReasonCode == "boundary_receipt_stale"
    assert boundary_stale.currentStep == "boundaries"


def test_boundary_policy_drift_invalidates_current_receipt(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_receipts.record(
            store, "boundaries", receipt("receipt-policy", digest="1" * 64), now=NOW
        )
        step = next(
            item
            for item in setup_status.load(
                store,
                owner_key_configured=True,
                memory=Memory(),
                toolgate=ToolGate(digest="2" * 64),
                router=Router(),
                now=NOW,
            ).steps
            if item.id == "boundaries"
        )
    assert step.state == "degraded"
    assert step.blockingReasonCode == "boundary_receipt_stale"
    assert "changed" in step.evidence[0].detail


@pytest.mark.parametrize("expires", [None, NOW + timedelta(days=31)])
def test_persisted_boundary_with_unbounded_expiry_is_stale(tmp_path, expires):
    with closing(Store(tmp_path / "pi.db")) as store:
        with store._connect() as db:
            db.execute(
                "INSERT INTO setup_evidence_receipts VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "boundaries",
                    1,
                    "receipt-legacy-boundary",
                    "conker.host-verifier",
                    "installation.primary",
                    "1" * 64,
                    NOW.timestamp(),
                    expires.timestamp() if expires else None,
                    NOW.timestamp(),
                ),
            )

        saved = setup_receipts.current(store, "boundaries", now=NOW)
        step = next(
            item
            for item in setup_status.load(
                store,
                owner_key_configured=True,
                memory=Memory(),
                toolgate=ToolGate(digest="1" * 64),
                router=Router(),
                now=NOW,
            ).steps
            if item.id == "boundaries"
        )

    assert saved is not None
    assert saved.state == "stale"
    assert step.state == "degraded"
    assert step.blockingReasonCode == "boundary_receipt_stale"


def test_sqlite_backup_preserves_receipts(tmp_path):
    source, target = tmp_path / "pi.db", tmp_path / "restored.db"
    with closing(Store(source)) as store:
        saved = setup_receipts.record(
            store, "boundaries", receipt("receipt-backup", digest="4" * 64), now=NOW
        )
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
    with closing(Store(target)) as restored:
        assert setup_receipts.current(restored, "boundaries", now=NOW) == saved


def test_receipt_history_is_append_only_and_gapless_in_sqlite(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        setup_receipts.record(
            store, "boundaries", receipt("receipt-immutable", digest="7" * 64), now=NOW
        )
        with store._connect() as db:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(
                    "UPDATE setup_evidence_receipts SET subject='changed' WHERE step='boundaries'"
                )
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute("DELETE FROM setup_evidence_receipts WHERE step='boundaries'")
            with pytest.raises(sqlite3.IntegrityError, match="revision must be monotonic"):
                db.execute(
                    "INSERT INTO setup_evidence_receipts VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        "boundaries",
                        3,
                        "receipt-gap",
                        "conker.host-verifier",
                        "installation.primary",
                        "8" * 64,
                        NOW.timestamp(),
                        None,
                        NOW.timestamp(),
                    ),
                )


def test_owner_api_records_and_reads_only_allowlisted_receipts(tmp_path, monkeypatch):
    store = Store(tmp_path / "pi.db")
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "admin-" + "a" * 32, raising=False)
    monkeypatch.setattr(api.app.state, "toolgate", ToolGate(digest="5" * 64), raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    monkeypatch.setattr(
        api.app.state,
        "gateway_key_hash",
        hashlib.sha256(("r" * 32).encode()).hexdigest(),
        raising=False,
    )
    client = TestClient(api.app)
    body = receipt(
        "receipt-api", digest="5" * 64, completed=datetime.now(UTC) - timedelta(minutes=1)
    ).model_dump(mode="json")
    try:
        path = "/setup/receipts/boundaries"
        assert client.post(path, json=body).status_code == 401
        assert (
            client.post(path, json=body, headers={"X-Pi-Gateway-Key": "r" * 32}).status_code == 401
        )
        created = client.post(path, json=body, headers={"X-Pi-Owner-Key": OWNER})
        assert created.status_code == 200
        assert created.json()["step"] == "boundaries"
        assert created.json()["state"] == "valid"
        assert client.get(path, headers={"X-Pi-Owner-Key": OWNER}).json() == created.json()
        assert (
            client.post(
                "/setup/receipts/security", json=body, headers={"X-Pi-Owner-Key": OWNER}
            ).status_code
            == 403
        )
    finally:
        store.close()

    for method in ("GET", "POST"):
        assert owner_allowed(method, "/setup/receipts/boundaries")
        assert owner_allowed(method, "/setup/receipts/protection")
        assert owner_allowed(method, "/setup/receipts/rehearsal") is (method == "GET")
        assert not owner_allowed(method, "/setup/receipts/security")
        assert not runtime_allowed(method, "/setup/receipts/boundaries")
    assert owner_allowed("GET", "/setup/boundaries")
    assert not owner_allowed("POST", "/setup/boundaries")


def test_api_rejects_malformed_digest_and_reports_conflict_code(tmp_path, monkeypatch):
    store = Store(tmp_path / "pi.db")
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", "admin-" + "a" * 32, raising=False)
    monkeypatch.setattr(api.app.state, "toolgate", ToolGate(digest="6" * 64), raising=False)
    monkeypatch.setattr(
        api.app.state, "owner_key_hash", hashlib.sha256(OWNER.encode()).hexdigest(), raising=False
    )
    client = TestClient(api.app)
    headers = {"X-Pi-Owner-Key": OWNER}
    try:
        observed = datetime.now(UTC) - timedelta(minutes=1)
        malformed = receipt("receipt-bad", completed=observed).model_dump(mode="json")
        malformed["evidenceDigest"] = "ABC"
        assert (
            client.post("/setup/receipts/boundaries", json=malformed, headers=headers).status_code
            == 422
        )

        good = receipt("receipt-conflict", digest="6" * 64, completed=observed).model_dump(
            mode="json"
        )
        assert (
            client.post("/setup/receipts/boundaries", json=good, headers=headers).status_code == 200
        )
        good["subject"] = "installation.other"
        conflict = client.post("/setup/receipts/boundaries", json=good, headers=headers)
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "replay_conflict"
    finally:
        store.close()
