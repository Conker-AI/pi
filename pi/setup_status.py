"""Read-only first-run setup status derived from authoritative evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import (
    agents,
    model_roles,
    setup_choices,
    setup_model_probes,
    setup_protection,
    setup_receipts,
    setup_rehearsal,
)

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


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Evidence(StrictModel):
    source: str = Field(min_length=1, max_length=80)
    status: Literal["ok", "missing", "degraded", "unknown"]
    revision: int | None = Field(default=None, ge=0)
    detail: str = Field(min_length=1, max_length=160)


class Step(StrictModel):
    id: StepId
    state: StepState
    required: bool
    prerequisites: list[StepId]
    blockingReasonCode: BlockingReasonCode | None
    evidence: list[Evidence]


class SetupStatus(StrictModel):
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


def _security(store, owner_key_configured):
    store_health = store.health()
    evidence = [
        _evidence(
            "gateway.owner-credential",
            "ok" if owner_key_configured else "missing",
            "Distinct owner-control credential is configured."
            if owner_key_configured
            else "Owner-control credential is not configured.",
        ),
        _evidence(
            "pi.store",
            "ok" if store_health.get("status") == "ok" else "degraded",
            "Pi durable store is available."
            if store_health.get("status") == "ok"
            else "Pi durable store could not be verified.",
        ),
    ]
    if store_health.get("status") != "ok":
        state, reason = "degraded", "durable_store_unavailable"
    elif owner_key_configured:
        state, reason = "complete", None
    else:
        state, reason = "blocked", "owner_channel_not_configured"
    return _step("security", state, True, evidence, reason)


def _companion(store):
    try:
        companion = agents.get(store, "companion")
        agents.AgentInput.model_validate(companion["configuration"])
        configured = companion["archived_at"] is None and companion["kind"] == "companion"
    except Exception:
        return _step(
            "companion",
            "degraded",
            True,
            [_evidence("pi.agent.companion", "degraded", "Companion record is unreadable.")],
            "companion_configuration_unavailable",
        )
    if not configured:
        return _step(
            "companion",
            "blocked",
            True,
            [_evidence("pi.agent.companion", "missing", "Durable Companion configuration is unavailable.", companion["revision"])],
            "companion_configuration_unavailable",
        )
    choice = setup_choices.current(store, "companion")
    reviewed = companion["revision"] > 1 or choice.choice == "accept"
    evidence = [
        _evidence(
            "pi.agent.companion",
            "ok",
            "Owner-reviewed Companion configuration is available."
            if reviewed
            else "The supplied Companion default has not been reviewed yet.",
            companion["revision"],
        )
    ]
    if choice.choice == "accept":
        evidence.append(
            _evidence(
                "setup.choice.companion",
                "ok",
                "The owner accepted the supplied Companion default.",
                choice.revision,
            )
        )
    return _step(
        "companion",
        "complete" if reviewed else "in_progress",
        True,
        evidence,
        None if reviewed else "companion_configuration_unreviewed",
    )


def _model(store, router):
    try:
        saved = model_roles.load(store)
    except Exception:
        return _step(
            "model",
            "degraded",
            True,
            [_evidence("pi.model-roles", "degraded", "Model configuration is unreadable.")],
            "model_provider_unavailable",
        )
    if saved["configuration"] is None:
        return _step(
            "model",
            "not_started",
            True,
            [
                _evidence(
                    "pi.model-roles",
                    "missing",
                    "No versioned model-role configuration has been saved.",
                    saved["revision"],
                )
            ],
            "model_configuration_missing",
        )

    configuration = saved["configuration"]
    answer = configuration["roleSettings"]["roles"]["answer"]
    selected = next(
        (model for model in configuration["models"] if model["id"] == answer["modelId"]), None
    )
    adapter = router.adapters().get(selected["providerId"]) if selected else None
    if adapter is None:
        live = {"status": "unavailable"}
    else:
        try:
            live = adapter.health()
        except Exception:
            live = {"status": "unavailable"}
    live_status = live.get("status")
    if live_status not in {"ok", "unverified"}:
        return _step(
            "model",
            "degraded",
            True,
            [
                _evidence(
                    "pi.model-roles",
                    "ok",
                    "Versioned answer-model configuration is valid.",
                    saved["revision"],
                ),
                _evidence(
                    "model.provider",
                    "degraded",
                    "Selected answer-model provider is unavailable.",
                ),
            ],
            "model_provider_unavailable",
        )
    receipt = setup_model_probes.current(store, saved["revision"], selected["id"])
    return _step(
        "model",
        "complete" if receipt is not None else "in_progress",
        True,
        [
            _evidence(
                "pi.model-roles",
                "ok",
                "Versioned answer-model configuration is valid.",
                saved["revision"],
            ),
            _evidence(
                "model.provider",
                "ok" if live_status == "ok" else "unknown",
                "Selected answer-model provider is available."
                if live_status == "ok"
                else "Selected answer-model provider is configured but requires a live test.",
            ),
            _evidence(
                "setup.model-probe",
                "ok" if receipt is not None else "missing",
                "The selected answer model produced a valid setup response."
                if receipt is not None
                else "The selected answer model has not produced a verified setup response.",
                receipt.configurationRevision if receipt is not None else saved["revision"],
            ),
        ],
        None if receipt is not None else "model_response_unverified",
    )


def _memory(store, memory):
    choice = setup_choices.current(store, "memory")
    if choice.choice == "skip":
        configured = memory.client is not None
        return _step(
            "memory",
            "skipped",
            False,
            [
                _evidence(
                    "memorygate",
                    "ok" if configured else "missing",
                    "MemoryGate is available but disabled by the owner default."
                    if configured
                    else "Long-term memory is not configured.",
                ),
                _evidence(
                    "setup.choice.memory",
                    "ok",
                    "Long-term memory is intentionally off for new conversations.",
                    choice.revision,
                ),
            ],
        )
    if memory.client is None:
        return _step(
            "memory",
            "degraded" if choice.choice == "include" else "not_started",
            False,
            [
                _evidence("memorygate", "missing", "Long-term memory is not configured."),
                _evidence(
                    "setup.choice.memory",
                    "ok" if choice.choice == "include" else "missing",
                    "The owner chose to use long-term memory."
                    if choice.choice == "include"
                    else "Choose whether new conversations may use long-term memory.",
                    choice.revision,
                ),
            ],
            "memory_unavailable" if choice.choice == "include" else "memory_not_configured",
        )
    try:
        health = memory.health()
    except Exception:
        health = {"status": "unavailable"}
    healthy = health.get("status") == "ok"
    if choice.choice == "undecided":
        return _step(
            "memory",
            "in_progress" if healthy else "degraded",
            False,
            [
                _evidence(
                    "setup.choice.memory",
                    "missing",
                    "Choose whether new conversations may use long-term memory.",
                    choice.revision,
                ),
                _evidence(
                    "memorygate",
                    "ok" if healthy else "degraded",
                    "MemoryGate is available."
                    if healthy
                    else "MemoryGate is not fully available.",
                ),
            ],
            "memory_choice_unreviewed" if healthy else "memory_unavailable",
        )
    return _step(
        "memory",
        "complete" if healthy else "degraded",
        False,
        [
            _evidence(
                "memorygate",
                "ok" if healthy else "degraded",
                "MemoryGate is configured and available."
                if healthy
                else "MemoryGate is configured but not fully available.",
            ),
            _evidence(
                "setup.choice.memory",
                "ok",
                "The owner chose to use long-term memory.",
                choice.revision,
            ),
        ],
        None if healthy else "memory_unavailable",
    )


def _speech_evidence(speech):
    if speech is None:
        return _evidence(
            "speech",
            "missing",
            "Voice is optional and not configured. Use conker speech configure on the host to enable it.",
        )
    try:
        capabilities = speech.capabilities()
        input_status = capabilities["stt"]["status"]
        output_status = capabilities["tts"]["status"]
    except Exception:
        return _evidence(
            "speech",
            "degraded",
            "Voice configuration could not be read. Check conker speech status on the host.",
        )
    if input_status == "unconfigured":
        return _evidence(
            "speech",
            "missing",
            "Voice is optional and not configured. Use conker speech configure on the host to enable it.",
        )
    if input_status == "unavailable" or output_status == "unavailable":
        return _evidence(
            "speech",
            "degraded",
            "Voice is configured but its latest operation was unavailable. Check conker speech status on the host.",
        )
    output = "speech replies are configured" if output_status in {"configured", "available"} else "replies remain text-only"
    return _evidence(
        "speech",
        "ok",
        f"Microphone turns are configured; {output}. Credentials remain host-owned.",
    )


def _capabilities(store, toolgate, speech=None):
    speech_evidence = _speech_evidence(speech)
    choice = setup_choices.current(store, "capabilities")
    if choice.choice == "skip":
        return _step(
            "capabilities",
            "skipped",
            False,
            [
                _evidence(
                    "toolgate",
                    "ok" if toolgate is not None else "missing",
                    "ToolGate is available but hidden from model turns by the owner default."
                    if toolgate is not None
                    else "ToolGate is not configured.",
                ),
                _evidence(
                    "setup.choice.capabilities",
                    "ok",
                    "Additional capabilities are intentionally off for model turns.",
                    choice.revision,
                ),
                speech_evidence,
            ],
        )
    if toolgate is None:
        return _step(
            "capabilities",
            "degraded" if choice.choice == "include" else "not_started",
            False,
            [
                _evidence("toolgate", "missing", "ToolGate is not configured."),
                _evidence(
                    "setup.choice.capabilities",
                    "ok" if choice.choice == "include" else "missing",
                    "The owner chose to connect available capabilities."
                    if choice.choice == "include"
                    else "Choose whether model turns may use available capabilities.",
                    choice.revision,
                ),
                speech_evidence,
            ],
            "toolgate_unavailable" if choice.choice == "include" else "toolgate_not_configured",
        )
    try:
        health = toolgate.health()
        tools = toolgate.tools() if health.get("status") == "ok" else []
    except Exception:
        health, tools = {"status": "unavailable"}, []
    if health.get("status") != "ok":
        state, status, detail, reason = (
            "degraded",
            "degraded",
            "ToolGate is configured but not fully available.",
            "toolgate_unavailable",
        )
    elif tools and choice.choice == "undecided":
        state, status, detail, reason = (
            "in_progress",
            "ok",
            f"ToolGate exposes {len(tools)} scoped {'capability' if len(tools) == 1 else 'capabilities'}; owner choice is pending.",
            "capability_choice_unreviewed",
        )
    elif tools:
        noun = "capability" if len(tools) == 1 else "capabilities"
        state, status, detail, reason = (
            "complete",
            "ok",
            f"ToolGate exposes {len(tools)} scoped {noun}.",
            None,
        )
    else:
        state, status, detail, reason = (
            "in_progress",
            "missing",
            "ToolGate is available but exposes no scoped capabilities.",
            "capability_catalog_empty",
        )
    evidence = [_evidence("toolgate", status, detail)]
    if choice.choice == "include":
        evidence.append(
            _evidence(
                "setup.choice.capabilities",
                "ok",
                "The owner chose to connect available capabilities.",
                choice.revision,
            )
        )
    elif choice.choice == "undecided":
        evidence.insert(
            0,
            _evidence(
                "setup.choice.capabilities",
                "missing",
                "Choose whether model turns may use available capabilities.",
                choice.revision,
            )
        )
    evidence.append(speech_evidence)
    return _step("capabilities", state, False, evidence, reason)


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
