"""Bounded first-run answer-model selection from server-owned adapters."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import model_roles, setup_model_probes


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Candidate(StrictModel):
    id: str = Field(min_length=1, max_length=200)
    providerId: str = Field(min_length=1, max_length=100)
    providerName: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=160)
    route: str = Field(min_length=1, max_length=300)
    status: Literal["ready", "unverified", "unavailable"]
    selected: bool
    execution: Literal["local", "hosted"]
    dataNotice: str = Field(min_length=1, max_length=240)
    costNotice: str = Field(min_length=1, max_length=240)


class Options(StrictModel):
    revision: int = Field(ge=0)
    candidates: list[Candidate] = Field(max_length=1000)


class Selection(StrictModel):
    candidateId: str = Field(min_length=1, max_length=200)
    expectedRevision: int = Field(ge=0)


class Activation(Selection):
    requestId: str = Field(min_length=1, max_length=128)


class Activated(StrictModel):
    revision: int = Field(gt=0)
    candidateId: str = Field(min_length=1, max_length=200)
    probe: setup_model_probes.ProbeReceipt


def _status(adapter) -> Literal["ready", "unverified", "unavailable"]:
    if adapter is None:
        return "unavailable"
    try:
        value = adapter.health().get("status")
    except Exception:
        return "unavailable"
    if value == "ok":
        return "ready"
    if value == "unverified":
        return "unverified"
    return "unavailable"


def options(store, router) -> Options:
    saved = model_roles.load(store)
    configuration = saved["configuration"]
    selected = None
    if configuration is not None:
        selected = configuration["roleSettings"]["roles"]["answer"]["modelId"]
    adapters = router.adapters()
    values: list[Candidate] = []
    seen: set[str] = set()

    if configuration is not None:
        providers = {item["id"]: item for item in configuration["providers"]}
        for model in configuration["models"]:
            provider = providers.get(model["providerId"])
            if provider is None:
                continue
            values.append(
                Candidate(
                    id=model["id"],
                    providerId=model["providerId"],
                    providerName=provider["name"],
                    name=model["name"],
                    route=model["route"],
                    status=_status(adapters.get(model["providerId"])),
                    selected=model["id"] == selected,
                    execution=(
                        "local"
                        if adapters.get(model["providerId"]) is getattr(router, "local", None)
                        else "hosted"
                    ),
                    dataNotice=(
                        "The setup test stays on this server."
                        if adapters.get(model["providerId"]) is getattr(router, "local", None)
                        else f"The setup test is sent to {provider['name']}."
                    ),
                    costNotice=(
                        "No provider charge."
                        if adapters.get(model["providerId"]) is getattr(router, "local", None)
                        else "Provider billing may apply."
                    ),
                )
            )
            seen.add(model["id"])

    local = getattr(router, "local", None)
    route = getattr(router, "local_model", "")
    if local is not None and isinstance(route, str) and route.strip():
        matching = next(
            (
                item
                for item in values
                if item.providerId == local.name and item.route == route
            ),
            None,
        )
        if matching is None:
            identity = "local-answer"
            suffix = 2
            while identity in seen:
                identity = f"local-answer-{suffix}"
                suffix += 1
            values.insert(
                0,
                Candidate(
                    id=identity,
                    providerId=local.name,
                    providerName="Local model",
                    name=route,
                    route=route,
                    status=_status(local),
                    selected=False,
                    execution="local",
                    dataNotice="The setup test stays on this server.",
                    costNotice="No provider charge.",
                ),
            )

    return Options(revision=saved["revision"], candidates=values)


def _disabled(role: str) -> dict:
    return {
        "enabled": False,
        "eligibleModelIds": [],
        "modelId": None,
        "timeoutMs": 2000 if role == "memory-ranking" else 120000,
        "failure": "stop",
        "fallbackModelId": None,
    }


def select(store, router, body: Selection) -> dict:
    current = options(store, router)
    if current.revision != body.expectedRevision:
        raise ValueError("Model settings changed; reload before choosing an answer model.")
    candidate = next((item for item in current.candidates if item.id == body.candidateId), None)
    if candidate is None:
        raise LookupError("The selected answer model is no longer available.")
    if candidate.status == "unavailable":
        raise RuntimeError("The selected answer model is not ready because it is unavailable.")

    saved = model_roles.load(store)
    configuration = saved["configuration"]
    if configuration is None:
        roles = {role: _disabled(role) for role in model_roles.ROLES}
        roles["answer"] = {
            **_disabled("answer"),
            "enabled": True,
            "eligibleModelIds": [candidate.id],
            "modelId": candidate.id,
        }
        configuration = {
            "providers": [
                {"id": candidate.providerId, "name": candidate.providerName, "enabled": True}
            ],
            "models": [
                {
                    "id": candidate.id,
                    "providerId": candidate.providerId,
                    "name": candidate.name,
                    "route": candidate.route,
                    "enabled": True,
                    "routingDescription": "",
                }
            ],
            "defaultModelId": candidate.id,
            "roleSettings": {"answerMode": "manual", "roles": roles},
        }
    else:
        configuration = model_roles.Configuration.model_validate(configuration).model_dump()
        provider = next(
            (item for item in configuration["providers"] if item["id"] == candidate.providerId),
            None,
        )
        if provider is None:
            configuration["providers"].append(
                {"id": candidate.providerId, "name": candidate.providerName, "enabled": True}
            )
        else:
            provider["enabled"] = True
        model = next(
            (item for item in configuration["models"] if item["id"] == candidate.id), None
        )
        if model is None:
            configuration["models"].append(
                {
                    "id": candidate.id,
                    "providerId": candidate.providerId,
                    "name": candidate.name,
                    "route": candidate.route,
                    "enabled": True,
                    "routingDescription": "",
                }
            )
        else:
            model["enabled"] = True
        answer = configuration["roleSettings"]["roles"]["answer"]
        answer.update(
            enabled=True,
            eligibleModelIds=list(dict.fromkeys([*answer["eligibleModelIds"], candidate.id])),
            modelId=candidate.id,
            failure="stop",
            fallbackModelId=None,
        )
        configuration["roleSettings"]["answerMode"] = "manual"
        configuration["defaultModelId"] = candidate.id

    return model_roles.save(
        store,
        model_roles.Update(
            expected_revision=body.expectedRevision,
            configuration=model_roles.Configuration.model_validate(configuration),
        ),
    )


def activate(store, router, body: Activation) -> Activated:
    """Select and prove one model as a single owner-control operation."""
    saved = select(
        store,
        router,
        Selection(candidateId=body.candidateId, expectedRevision=body.expectedRevision),
    )
    receipt = setup_model_probes.probe(
        store,
        router,
        setup_model_probes.ProbeInput(
            requestId=body.requestId,
            candidateId=body.candidateId,
            expectedRevision=saved["revision"],
        ),
    )
    return Activated(revision=saved["revision"], candidateId=body.candidateId, probe=receipt)
