"""Shared builders for the gateway setup-control tests: a gateway config and a stored receipt."""

from __future__ import annotations

from gateway.api import Config


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
