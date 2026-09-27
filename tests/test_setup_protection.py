import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi import api, setup_protection, setup_receipts, setup_status
from pi.browser_contract import owner_allowed, runtime_allowed
from pi.store import Store

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
OWNER = "setup-protection-owner-" + "o" * 32


def policy_input(request_id="protection-policy-1", revision=0, destination="/mnt/conker"):
    return setup_protection.PolicyInput(
        requestId=request_id,
        destination=destination,
        retentionCopies=7,
        expectedRevision=revision,
    )


def receipt(policy, *, expected=0):
    return setup_receipts.ReceiptInput(
        receiptId=f"protection-receipt-{policy.revision}",
        source="conker.coordinated-backup",
        subject=setup_protection.receipt_subject(policy),
        evidenceDigest=f"{policy.revision}" * 64,
        completedAt=NOW,
        expiresAt=NOW + timedelta(days=30),
        expectedRevision=expected,
    )


def test_policy_is_revisioned_replay_safe_and_append_only(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        assert setup_protection.current(store).revision == 0
        first = setup_protection.save(store, policy_input(), now=NOW)
        assert first.destinationKind == "mounted_off_machine"
        assert first.destination == "/mnt/conker"
        assert first.retentionCopies == 7
        assert len(first.policyDigest) == 64
        assert setup_protection.save(store, policy_input(), now=NOW) == first

        with pytest.raises(setup_protection.ProtectionError) as conflict:
            setup_protection.save(
                store, policy_input(destination="/media/other"), now=NOW
            )
        assert conflict.value.code == "replay_conflict"

        with pytest.raises(setup_protection.ProtectionError) as stale:
            setup_protection.save(
                store,
                policy_input("protection-policy-2", 0, "/media/conker"),
                now=NOW,
            )
        assert stale.value.code == "revision_conflict"

        with store._connect() as db:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute("UPDATE setup_protection_policies SET retention_copies=2")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute("DELETE FROM setup_protection_policies")


@pytest.mark.parametrize(
    "destination",
    ["relative/path", "/", "/mnt/../secret", "/mnt/conker/", "/mnt/conker\nother"],
)
def test_policy_rejects_ambiguous_or_unsafe_host_paths(destination):
    with pytest.raises(ValidationError):
        policy_input(destination=destination)


def test_protection_receipt_is_bound_to_current_policy(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        first = setup_protection.save(store, policy_input(), now=NOW)
        saved = setup_receipts.record(store, "protection", receipt(first), now=NOW)
        assert setup_status._receipt_step(store, "protection", now=NOW).state == "complete"

        setup_protection.save(
            store,
            policy_input("protection-policy-2", 1, "/media/conker"),
            now=NOW + timedelta(minutes=1),
        )
        stale = setup_status._receipt_step(store, "protection", now=NOW)
        assert stale.state == "degraded"
        assert stale.blockingReasonCode == "protection_receipt_stale"
        assert "changed" in stale.evidence[0].detail
        assert saved.state == "valid"


def test_owner_api_configures_policy_and_accepts_only_matching_host_evidence(
    tmp_path, monkeypatch
):
    store = Store(tmp_path / "pi.db")
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(
        api.app.state,
        "owner_key_hash",
        hashlib.sha256(OWNER.encode()).hexdigest(),
        raising=False,
    )
    client = TestClient(api.app)
    headers = {"X-Pi-Owner-Key": OWNER}
    try:
        assert client.get("/setup/protection").status_code == 401
        empty = client.get("/setup/protection", headers=headers)
        assert empty.status_code == 200 and empty.json()["revision"] == 0
        saved = client.post(
            "/setup/protection",
            headers=headers,
            json=policy_input().model_dump(mode="json"),
        )
        assert saved.status_code == 200
        policy = setup_protection.Policy.model_validate(saved.json(), strict=False)
        observed = datetime.now(UTC) - timedelta(minutes=1)
        current_receipt = receipt(policy).model_copy(
            update={
                "completedAt": observed,
                "expiresAt": observed + timedelta(days=30),
            }
        )

        mismatched = current_receipt.model_copy(
            update={"subject": "installation.repository.protection.999.deadbeefdeadbeef"}
        )
        refused = client.post(
            "/setup/receipts/protection",
            headers=headers,
            json=mismatched.model_dump(mode="json"),
        )
        assert refused.status_code == 409
        accepted = client.post(
            "/setup/receipts/protection",
            headers=headers,
            json=current_receipt.model_dump(mode="json"),
        )
        assert accepted.status_code == 200
    finally:
        store.close()

    for method in ("GET", "POST"):
        assert owner_allowed(method, "/setup/protection")
        assert not runtime_allowed(method, "/setup/protection")
