"""Model calls for Conker's own services, through the owner's model roles.

MemoryGate and ToolGate send a prompt for one of their roles; Pi answers with the model the owner
chose for that role, on the same path as Pi's own role calls (routing, fallback, the providers'
paid-model opt-in, local capacity and cancellation). Until the owner picks a model for a role, it
uses the local model, so nothing leaves the server. Each service may use only its own roles,
fixed here. Pi keeps no prompt and no answer: only which service called, the role, the model, the
usage and how the call ended.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
import uuid

from pydantic import Field

from pi.core.errors import ServiceError
from pi.core.models import Strict
from pi.models import providers
from pi.models import roles as model_roles
from pi.models.providers import Message, ProviderUnavailable
from pi.turns import inference_control

# Which roles each service may call. Fixed in code: a key cannot widen it.
SERVICES = {"memorygate": ("memory-sorting",), "toolgate": ("tool-text",)}
# Memory sorting runs behind the owner's back, so a reply waiting for the local model goes first.
# ToolGate's text is part of an action someone is waiting on.
BACKGROUND = {"memorygate"}
LOCAL_MODEL_ID = "local-model"
# Calls a service may have running at once. Each waits for a model, so more would only hold
# Pi's request threads; the next one is told to try again.
IN_FLIGHT = 4
_slots = {service: threading.BoundedSemaphore(IN_FLIGHT) for service in SERVICES}
# An answer this many characters per token longer than max_tokens ignored the limit.
CHARACTERS_PER_TOKEN = 16
KEEP_SECONDS = 30 * 86400
SCHEMA = """
CREATE TABLE IF NOT EXISTS service_model_calls (
 id TEXT PRIMARY KEY, service TEXT NOT NULL, role TEXT NOT NULL, request_id TEXT NOT NULL,
 provider_id TEXT, model_id TEXT, provider TEXT NOT NULL, model TEXT NOT NULL,
 actual_model TEXT, status TEXT NOT NULL CHECK(status IN ('pending','completed','failed')),
 failure_kind TEXT, started_at REAL NOT NULL, ended_at REAL, elapsed_ms INTEGER,
 input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL
);
CREATE INDEX IF NOT EXISTS service_model_calls_started ON service_model_calls(started_at);
"""


class InferenceError(ServiceError):
    pass


class InferenceRequest(Strict):
    role: str = Field(
        min_length=1,
        max_length=64,
        description="memory-sorting (MemoryGate) or tool-text (ToolGate): the caller's own role.",
    )
    system: str = Field(max_length=16000)
    prompt: str = Field(min_length=1, max_length=64000)
    max_tokens: int = Field(ge=8, le=4096)
    as_json: bool = Field(
        default=False, alias="json", description="Ask the model for JSON where it can."
    )
    request_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{16,128}$")


class InferenceAnswer(Strict):
    text: str
    model: str
    provider: str


def key_hashes(raw: str) -> dict[str, str]:
    """PI_SERVICE_KEYS_SHA256: a JSON object of service name to the SHA-256 hex of its key."""
    raw = raw.strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise ValueError(
            'PI_SERVICE_KEYS_SHA256 must be a JSON object like {"memorygate": "<sha256 hex>"}.'
        )
    found = {}
    for service, digest in value.items():
        if service not in SERVICES:
            raise ValueError(
                f"PI_SERVICE_KEYS_SHA256 names {service!r}; the services are "
                + ", ".join(SERVICES)
                + "."
            )
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest.lower()):
            raise ValueError(f"PI_SERVICE_KEYS_SHA256 needs a SHA-256 hex digest for {service}.")
        found[service] = digest.lower()
    if len(set(found.values())) != len(found):
        raise ValueError("PI_SERVICE_KEYS_SHA256 needs a different key for each service.")
    return found


def authenticate(hashes: dict[str, str], key: str | None) -> str | None:
    """The service this key belongs to. Every hash is compared, in constant time."""
    presented = hashlib.sha256((key or "").encode()).hexdigest().encode()
    found = None
    for service, expected in sorted(hashes.items()):
        if secrets.compare_digest(presented, expected.encode()):
            found = service
    return found if key else None


def _local(role: str, router, timeout_ms: int) -> dict:
    """Settings that send this one role to the local model; never saved."""
    local, route = getattr(router, "local", None), getattr(router, "local_model", "")
    if local is None or not isinstance(route, str) or not route.strip():
        raise InferenceError(
            "no_model", "No model is chosen for this role and there is no local model.", 503
        )
    off = dict(
        enabled=False,
        eligibleModelIds=[],
        modelId=None,
        timeoutMs=model_roles.default_timeout_ms(role),
        failure="stop",
        fallbackModelId=None,
    )
    return model_roles.Configuration.model_validate(
        dict(
            providers=[dict(id=local.name, name="Local model", enabled=True)],
            models=[
                dict(
                    id=LOCAL_MODEL_ID,
                    providerId=local.name,
                    name=route,
                    route=route,
                    enabled=True,
                )
            ],
            defaultModelId=LOCAL_MODEL_ID,
            roleSettings=dict(
                answerMode="manual",
                roles={
                    **{name: dict(off) for name in model_roles.ROLES},
                    role: {
                        **off,
                        "enabled": True,
                        "eligibleModelIds": [LOCAL_MODEL_ID],
                        "modelId": LOCAL_MODEL_ID,
                        "timeoutMs": timeout_ms,
                    },
                },
            ),
        )
    ).model_dump()


def settings(store, router, role: str) -> dict:
    """The owner's settings once a model is picked for this role, else the local model."""
    configuration = model_roles.load(store)["configuration"]
    assignment = configuration["roleSettings"]["roles"][role] if configuration else None
    if assignment is not None and assignment["enabled"]:
        return configuration
    timeout = assignment["timeoutMs"] if assignment else model_roles.LOCAL_TIMEOUT_MS
    return _local(role, router, timeout)


def _usable(configuration: dict, role: str, adapters: dict) -> bool:
    assignment = configuration["roleSettings"]["roles"][role]
    models = {item["id"]: item for item in configuration["models"]}
    wanted = [assignment["modelId"]]
    if assignment["failure"] == "fallback":
        wanted.append(assignment["fallbackModelId"])
    return any(
        identity in models
        and callable(
            getattr(adapters.get(models[identity]["providerId"]), "complete_bounded", None)
        )
        for identity in wanted
    )


def _failure(exc) -> str:
    token = inference_control.current()
    reason = token.reason if token is not None else None
    if reason == "deadline":
        return "timeout"
    if reason == "preempted":
        return "preempted"
    if reason is not None or isinstance(exc, inference_control.InferenceCancelled):
        return "cancelled"
    if getattr(exc, "reason", None) in ("ReadTimeout", "ConnectTimeout", "WriteTimeout"):
        return "timeout"
    return "provider_error"


def _counter(value, limit=2**31 - 1):
    return value if type(value) is int and 0 <= value <= limit else None


def _label(value, limit):
    return value if isinstance(value, str) and 1 <= len(value) <= limit else None


def _observer(store, service: str, request_id: str):
    """Record each provider attempt the role call makes, without its text."""

    def observe(invoke, *, provider, model, role, provider_id=None, model_id=None):
        identity = uuid.uuid4().hex
        now = time.time()
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM service_model_calls WHERE started_at<?", (now - KEEP_SECONDS,))
            db.execute(
                "INSERT INTO service_model_calls(id,service,role,request_id,provider_id,model_id,"
                "provider,model,status,started_at) VALUES(?,?,?,?,?,?,?,?,'pending',?)",
                (
                    identity,
                    service,
                    role,
                    request_id,
                    _label(provider_id, 100),
                    _label(model_id, 200),
                    _label(provider, 100) or "unknown",
                    _label(model, 300) or "unknown",
                    now,
                ),
            )
            db.commit()
        started = time.monotonic()
        try:
            completion = invoke()
        except Exception as exc:
            with store._connect() as db:
                db.execute(
                    "UPDATE service_model_calls SET status='failed',failure_kind=?,ended_at=?,"
                    "elapsed_ms=? WHERE id=?",
                    (
                        _failure(exc),
                        time.time(),
                        round((time.monotonic() - started) * 1000),
                        identity,
                    ),
                )
                db.commit()
            raise
        cost = completion.cost_usd
        with store._connect() as db:
            db.execute(
                "UPDATE service_model_calls SET status='completed',actual_model=?,ended_at=?,"
                "elapsed_ms=?,input_tokens=?,output_tokens=?,cost_usd=? WHERE id=?",
                (
                    _label(completion.model, 300),
                    time.time(),
                    round((time.monotonic() - started) * 1000),
                    _counter(completion.input_tokens),
                    _counter(completion.output_tokens),
                    float(cost) if type(cost) in (int, float) and 0 <= cost < 1e9 else None,
                    identity,
                ),
            )
            db.commit()
        return completion

    return observe


def call(store, router, service: str, body: InferenceRequest) -> InferenceAnswer:
    """One role call for one request: no retries beyond the fallback the owner set."""
    if body.role not in SERVICES.get(service, ()):
        raise InferenceError("role_not_allowed", "This service may not use that model role.", 403)
    configuration = settings(store, router, body.role)
    adapters = router.adapters()
    if not _usable(configuration, body.role, adapters):
        raise InferenceError("no_model", "The model chosen for this role is not available.", 503)
    if not _slots[service].acquire(blocking=False):
        raise InferenceError("unavailable", "Too many calls from this service at once.", 503)
    try:
        return _answer(store, service, body, configuration, adapters)
    finally:
        _slots[service].release()


def _answer(store, service, body, configuration, adapters) -> InferenceAnswer:
    assignment = configuration["roleSettings"]["roles"][body.role]
    attempts = 2 if assignment["failure"] == "fallback" else 1
    messages = [Message("system", body.system)] if body.system else []
    messages.append(Message("user", body.prompt))
    token = inference_control.Token()
    try:
        with (
            providers.output(providers.Output(body.as_json, body.max_tokens, think=False)),
            inference_control.bind(token, background=service in BACKGROUND),
            # Each model gets the role's wait; the request never runs longer than that.
            token.deadline(assignment["timeoutMs"] * attempts / 1000),
        ):
            result = model_roles.dispatch(
                configuration,
                body.role,
                messages,
                adapters,
                guard=inference_control.check,
                observe_call=_observer(store, service, body.request_id),
            )
    except (ProviderUnavailable, inference_control.InferenceCancelled, ValueError, TypeError):
        raise InferenceError(
            "unavailable", "The model did not answer. Try again later.", 503
        ) from None
    completion = result["completion"]
    if len(completion.text) > body.max_tokens * CHARACTERS_PER_TOKEN:
        raise InferenceError("unavailable", "The model answered past max_tokens.", 503)
    return InferenceAnswer(
        text=completion.text, model=completion.model, provider=result["providerId"]
    )


def usage(store, limit: int = 50) -> dict:
    """Recent service calls and the last day's totals per service and role; never any text."""
    since = time.time() - 86400
    with store._connect() as db:
        rows = db.execute(
            "SELECT id,service,role,request_id,provider_id,model_id,provider,model,actual_model,"
            "status,failure_kind,started_at,ended_at,elapsed_ms,input_tokens,output_tokens,cost_usd "
            "FROM service_model_calls ORDER BY started_at DESC,id DESC LIMIT ?",
            (max(1, min(limit, 200)),),
        ).fetchall()
        totals = db.execute(
            "SELECT service,role,COUNT(*) AS calls,SUM(status='failed') AS failed,"
            "SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,"
            "SUM(cost_usd) AS cost_usd,COUNT(cost_usd) AS priced "
            "FROM service_model_calls WHERE started_at>=? GROUP BY service,role "
            "ORDER BY service,role",
            (since,),
        ).fetchall()
    return {
        "calls": [dict(row) for row in rows],
        "lastDay": [
            {
                "service": row["service"],
                "role": row["role"],
                "calls": row["calls"],
                "failed": row["failed"] or 0,
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                # A local call reports no price: the total is unknown, not zero.
                "cost_usd": row["cost_usd"] if row["priced"] == row["calls"] else None,
            }
            for row in totals
        ],
    }
