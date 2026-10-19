"""Setup status shapes: step ids, states, reasons and the models built from them."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from pi.core.models import Strict

StepId = Literal[
    "security",
    "companion",
    "model",
    "memory",
    "capabilities",
    "boundaries",
    "protection",
    "rehearsal",
]


StepState = Literal[
    "not_started",
    "in_progress",
    "blocked",
    "skipped",
    "complete",
    "degraded",
]


WorkflowState = Literal["in_progress", "blocked", "complete", "degraded"]


BlockingReasonCode = Literal[
    "owner_channel_not_configured",
    "durable_store_unavailable",
    "companion_configuration_unavailable",
    "companion_configuration_unreviewed",
    "model_configuration_missing",
    "model_response_unverified",
    "model_provider_unavailable",
    "memory_choice_unreviewed",
    "memory_not_configured",
    "memory_unavailable",
    "capability_choice_unreviewed",
    "toolgate_not_configured",
    "toolgate_unavailable",
    "capability_catalog_empty",
    "boundary_receipt_unavailable",
    "protection_receipt_unavailable",
    "rehearsal_receipt_unavailable",
    "boundary_receipt_stale",
    "protection_receipt_stale",
    "rehearsal_receipt_stale",
]


RecommendedOperation = Literal[
    "configure_owner_channel",
    "repair_durable_store",
    "repair_companion_configuration",
    "configure_companion",
    "configure_model",
    "test_model",
    "repair_model_provider",
    "configure_memory",
    "repair_memory",
    "configure_capabilities",
    "repair_toolgate",
    "review_boundaries",
    "verify_protection",
    "run_rehearsal",
]


PREREQUISITES: dict[StepId, tuple[StepId, ...]] = {
    "security": (),
    "companion": ("security",),
    "model": ("security", "companion"),
    "memory": ("security", "companion", "model"),
    "capabilities": ("security", "companion", "model"),
    "boundaries": ("security", "companion", "model"),
    "protection": ("security",),
    "rehearsal": ("security", "companion", "model", "boundaries", "protection"),
}


OPERATIONS: dict[BlockingReasonCode, RecommendedOperation] = {
    "owner_channel_not_configured": "configure_owner_channel",
    "durable_store_unavailable": "repair_durable_store",
    "companion_configuration_unavailable": "repair_companion_configuration",
    "companion_configuration_unreviewed": "configure_companion",
    "model_configuration_missing": "configure_model",
    "model_response_unverified": "test_model",
    "model_provider_unavailable": "repair_model_provider",
    "memory_choice_unreviewed": "configure_memory",
    "memory_not_configured": "configure_memory",
    "memory_unavailable": "repair_memory",
    "capability_choice_unreviewed": "configure_capabilities",
    "toolgate_not_configured": "configure_capabilities",
    "toolgate_unavailable": "repair_toolgate",
    "capability_catalog_empty": "configure_capabilities",
    "boundary_receipt_unavailable": "review_boundaries",
    "protection_receipt_unavailable": "verify_protection",
    "rehearsal_receipt_unavailable": "run_rehearsal",
    "boundary_receipt_stale": "review_boundaries",
    "protection_receipt_stale": "verify_protection",
    "rehearsal_receipt_stale": "run_rehearsal",
}


class Evidence(Strict):
    source: str = Field(min_length=1, max_length=80)
    status: Literal["ok", "missing", "degraded", "unknown"]
    revision: int | None = Field(default=None, ge=0)
    detail: str = Field(min_length=1, max_length=160)


class Step(Strict):
    id: StepId
    state: StepState
    required: bool
    prerequisites: list[StepId]
    blockingReasonCode: BlockingReasonCode | None
    evidence: list[Evidence]


class SetupStatus(Strict):
    schemaVersion: Literal[1] = 1
    workflow: Literal["first-run"] = "first-run"
    state: WorkflowState
    currentStep: StepId | None
    recommendedNextOperation: RecommendedOperation | None
    generatedAt: datetime
    steps: list[Step]


def _evidence(source, status, detail, revision=None):
    return Evidence(source=source, status=status, detail=detail, revision=revision)


def _step(identity, state, required, evidence, reason=None):
    return Step(
        id=identity,
        state=state,
        required=required,
        prerequisites=list(PREREQUISITES[identity]),
        blockingReasonCode=reason,
        evidence=evidence,
    )
