"""Setup receipt steps: boundaries, protection and rehearsal."""

from __future__ import annotations

from pi.setup import protection as setup_protection
from pi.setup import receipts as setup_receipts
from pi.setup import rehearsal as setup_rehearsal
from pi.setup.status_models import _evidence, _step


def _receipt_step(store, identity, *, now, toolgate=None):
    labels = {
        "boundaries": ("boundary_receipt_unavailable", "boundary_receipt_stale"),
        "protection": ("protection_receipt_unavailable", "protection_receipt_stale"),
        "rehearsal": ("rehearsal_receipt_unavailable", "rehearsal_receipt_stale"),
    }
    missing_reason, stale_reason = labels[identity]
    protection_policy = setup_protection.current(store) if identity == "protection" else None
    if protection_policy is not None and protection_policy.revision == 0:
        return _step(
            identity,
            "not_started",
            True,
            [
                _evidence(
                    "setup.protection.policy",
                    "missing",
                    "Choose a mounted off-machine backup destination and retention before the first backup.",
                )
            ],
            missing_reason,
        )
    try:
        receipt = setup_receipts.current(store, identity, now=now)
    except Exception:
        return _step(
            identity,
            "degraded",
            True,
            [
                _evidence(
                    f"setup.receipt.{identity}",
                    "degraded",
                    "Setup evidence receipts could not be read from Pi's durable store.",
                )
            ],
            missing_reason,
        )
    if receipt is None:
        return _step(
            identity,
            "not_started",
            True,
            [
                _evidence(
                    f"setup.receipt.{identity}",
                    "missing",
                    f"No owner-attested {identity} evidence receipt is recorded.",
                )
            ],
            missing_reason,
        )
    if receipt.state == "stale":
        return _step(
            identity,
            "degraded",
            True,
            [
                _evidence(
                    f"setup.receipt.{identity}",
                    "degraded",
                    f"Recorded {identity} evidence for {receipt.subject} is stale.",
                    receipt.revision,
                )
            ],
            stale_reason,
        )
    if identity == "boundaries":
        try:
            current_policy = toolgate.policy_summary() if toolgate is not None else None
        except Exception:
            current_policy = None
        if current_policy is None:
            return _step(
                identity,
                "degraded",
                True,
                [
                    _evidence(
                        "toolgate.policy",
                        "degraded",
                        "Current ToolGate policy could not be compared with the recorded review.",
                        receipt.revision,
                    )
                ],
                stale_reason,
            )
        if current_policy["digest"] != receipt.evidenceDigest:
            return _step(
                identity,
                "degraded",
                True,
                [
                    _evidence(
                        "toolgate.policy",
                        "degraded",
                        "ToolGate policy changed after the recorded review.",
                        receipt.revision,
                    )
                ],
                stale_reason,
            )
    if identity == "rehearsal" and not setup_rehearsal.receipt_matches_current(store, receipt):
        return _step(
            identity,
            "degraded",
            True,
            [
                _evidence(
                    "setup.receipt.rehearsal",
                    "degraded",
                    "Rehearsal evidence no longer matches the current memory choice or workflow proofs.",
                    receipt.revision,
                )
            ],
            stale_reason,
        )
    if (
        identity == "protection"
        and protection_policy is not None
        and receipt.subject != setup_protection.receipt_subject(protection_policy)
    ):
        return _step(
            identity,
            "degraded",
            True,
            [
                _evidence(
                    "setup.protection.policy",
                    "degraded",
                    "Backup policy changed after the recorded protection evidence.",
                    protection_policy.revision,
                )
            ],
            stale_reason,
        )
    return _step(
        identity,
        "complete",
        True,
        [
            _evidence(
                f"setup.receipt.{identity}",
                "ok",
                f"Owner-attested {identity} evidence for {receipt.subject} is current.",
                receipt.revision,
            )
        ],
    )
