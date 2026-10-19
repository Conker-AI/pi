"""Read-only first-run setup status derived from authoritative evidence."""

from __future__ import annotations

from datetime import UTC, datetime

from pi.setup.status_models import OPERATIONS, SetupStatus
from pi.setup.status_receipts import _receipt_step
from pi.setup.status_steps import _capabilities, _companion, _memory, _model, _security


def _workflow_state(steps):
    required = [step.state for step in steps if step.required]
    if "blocked" in required:
        return "blocked"
    if "degraded" in required:
        return "degraded"
    if all(state in {"complete", "skipped"} for state in required):
        return "complete"
    return "in_progress"


def _current_step(steps):
    states = {step.id: step.state for step in steps}
    terminal = {"complete", "skipped"}
    for step in steps:
        if step.state not in terminal and all(
            states[item] in terminal for item in step.prerequisites
        ):
            return step
    return None


def load(store, *, owner_key_configured, memory, toolgate, router, speech=None, now=None):
    observed = (now or datetime.now(UTC)).astimezone(UTC)
    steps = [
        _security(store, owner_key_configured),
        _companion(store),
        _model(store, router),
        _memory(store, memory),
        _capabilities(store, toolgate, speech),
        _receipt_step(store, "boundaries", now=observed, toolgate=toolgate),
        _receipt_step(store, "protection", now=observed),
        _receipt_step(store, "rehearsal", now=observed),
    ]
    current = _current_step(steps)
    return SetupStatus(
        state=_workflow_state(steps),
        currentStep=current.id if current else None,
        recommendedNextOperation=OPERATIONS.get(current.blockingReasonCode) if current else None,
        generatedAt=observed,
        steps=steps,
    )
