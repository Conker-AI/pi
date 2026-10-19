"""Setup step checks: each turns current evidence into one step's state."""

from __future__ import annotations

from pi.agents import profiles as agents
from pi.models import roles as model_roles
from pi.setup import choices as setup_choices
from pi.setup import model_probes as setup_model_probes
from pi.setup import models as setup_models
from pi.setup.status_models import _evidence, _step


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
            "pi.conversation.store",
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
            [
                _evidence(
                    "pi.agent.companion",
                    "missing",
                    "Durable Companion configuration is unavailable.",
                    companion["revision"],
                )
            ],
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
    enabled = (
        selected
        and selected["enabled"]
        and answer["enabled"]
        and any(
            item["id"] == selected["providerId"] and item["enabled"]
            for item in configuration["providers"]
        )
    )
    readiness = (
        setup_models.availability(adapter, [selected["route"]]).get(selected["route"])
        if enabled
        else "unavailable"
    )
    live_status = {"ready": "ok", "unverified": "unverified"}.get(readiness, "unavailable")
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
                    "Selected answer model or its provider is unavailable.",
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
                    "MemoryGate is available." if healthy else "MemoryGate is not fully available.",
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
    output = (
        "speech replies are configured"
        if output_status in {"configured", "available"}
        else "replies remain text-only"
    )
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
            ),
        )
    evidence.append(speech_evidence)
    return _step("capabilities", state, False, evidence, reason)
