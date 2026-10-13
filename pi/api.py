"""Pi's HTTP surface.

The worker API behind the authenticated browser gateway. Every route is Pi's own state -
sessions, messages, turns - because Pi coordinates and never owns anything else:
memory belongs to MemoryGate, actions to ToolGate, machine truth to SystemGate.
Tool calls arrive in #29 and go out through ToolGate, never from here.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import (
    activity_api,
    agents,
    agents_api,
    artifacts_api,
    attachments_api,
    calls,
    calls_api,
    characters_api,
    collaboration_api,
    context_api,
    context_controls,
    context_retrieval,
    continuity_api,
    conversation_search,
    drafts_api,
    filesystem_api,
    filesystem_reads,
    jobs_api,
    memory_api,
    memory_corrections,
    memory_proposals,
    memory_proposals_api,
    message_forks,
    model_evaluations,
    model_evaluations_api,
    model_roles_api,
    owner_preferences_api,
    project_sources,
    projects_api,
    proposals,
    proposals_api,
    provider_secrets,
    response_retries,
    response_versions,
    session_settings,
    session_settings_api,
    sessions_api,
    setup_status_api,
    speech,
    system_actions,
    system_actions_api,
    system_inventory,
    system_inventory_api,
    tasks,
    tasks_api,
    team_execution,
    team_execution_api,
    turn_queue,
    turn_steering,
)
from .browser_contract import runtime_allowed
from .direct_providers import configured as configured_direct_providers
from .job_execution import PublishedJobs
from .job_worker import JobWorker
from .loop import Loop
from .memory import Memory, MemoryClient
from .openrouter import OpenRouterProvider
from .providers import OllamaProvider
from .queue_worker import QueueWorker
from .routing import Router
from .store import Store
from .toolgate import ToolGateClient

log = logging.getLogger("pi")

SERVICE_VERSION = "0.5.6"
HEALTHY = {"ok", "not_configured"}
HEALTH_CACHE_SECONDS = 5.0

_health_cache: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    admin_key = os.environ.get("PI_ADMIN_KEY", "").strip()
    runtime_hash = os.environ.get("PI_GATEWAY_KEY_SHA256", "").strip()
    owner_hash = os.environ.get("PI_OWNER_KEY_SHA256", "").strip()
    if owner_hash and (
        len(owner_hash) != 64
        or any(c not in "0123456789abcdef" for c in owner_hash)
        or owner_hash in {runtime_hash, hashlib.sha256(admin_key.encode()).hexdigest()}
    ):
        raise RuntimeError("PI_OWNER_KEY_SHA256 must identify a distinct owner-control credential.")
    if runtime_hash and (
        len(runtime_hash) != 64 or any(c not in "0123456789abcdef" for c in runtime_hash)
    ):
        raise RuntimeError(
            "Set PI_GATEWAY_KEY_SHA256 to the gateway credential's SHA-256 hex digest, "
            "then restart Pi."
        )
    # Secure by default, or refuse to start. Never fall back to open - the rule
    # has a scar behind it, see ADR-0005.
    if len(admin_key) < 16:
        raise RuntimeError(
            "PI_ADMIN_KEY is required and must be at least 16 characters.\n\n"
            "Fix, in the directory holding docker-compose.yml:\n\n"
            '    echo "PI_ADMIN_KEY=$(openssl rand -base64 24)" >> .env\n'
            "    docker compose up -d pi\n"
        )
    try:
        provider_environment = provider_secrets.load(os.environ)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from None
    speech_client = speech.SpeechClient(
        url=os.environ.get("PI_SPEECH_URL", "").strip(),
        key=provider_environment.get("PI_SPEECH_KEY", "").strip(),
        stt_model=os.environ.get("PI_STT_MODEL", "").strip(),
        tts_model=os.environ.get("PI_TTS_MODEL", "").strip(),
        voice=os.environ.get("PI_TTS_VOICE", "").strip(),
        character_voice=os.environ.get("PI_SPEECH_CHARACTER_VOICE", "unsupported").strip(),
        audio_decoder=os.environ.get("PI_AUDIO_DECODER_PATH", "").strip(),
        timeout=_seconds("PI_SPEECH_TIMEOUT_S", 30.0),
    )
    store = Store(os.environ.get("PI_DB_PATH", "/data/pi.db"))
    # A turn that was running when the process died did not finish. Saying
    # nothing would leave the owner looking at a request that vanished.
    interrupted = store.mark_interrupted_turns()
    model_evaluations.recover_interrupted(store)
    context_retrieval.recover_interrupted(store)
    team_execution.recover_interrupted(store)
    memory_proposals.recover_interrupted(store)
    proposals.recover_interrupted(store)
    calls.recover(store)
    system_inventory.recover(store)
    filesystem_reads.recover(store)
    system_actions.recover(store)

    # Local inference is slow on modest hardware and costs nothing to wait for,
    # so the ceiling is generous. It exists to catch a hung server, not to give
    # up on a model that is still thinking - a timeout that fires on a working
    # model turns "slow" into "failed", which is a lie about what happened.
    local = OllamaProvider(
        os.environ.get("PI_OLLAMA_URL", "http://ollama:11434"),
        timeout=_seconds("PI_LOCAL_TIMEOUT_S", 600.0),
        model=os.environ.get("PI_MODEL", "qwen3:4b"),
    )

    # Free by default: a fresh install works with no payment and no key. The
    # hosted provider only exists if one was supplied, and even then it refuses
    # paid models unless PI_ALLOW_PAID_MODELS says otherwise - spending is a
    # deliberate act, never a default or a typo.
    openrouter_key = provider_environment.get("PI_OPENROUTER_KEY", "").strip()
    hosted = None
    if openrouter_key:
        hosted = OpenRouterProvider(
            openrouter_key,
            allow_paid=os.environ.get("PI_ALLOW_PAID_MODELS", "").strip() in {"1", "true", "yes"},
            timeout=_seconds("PI_HOSTED_TIMEOUT_S", 180.0),
        )

    # The only way Pi acts on the world. Without a key it acts on nothing,
    # which is a working install rather than a broken one - Conker still talks
    # and still remembers.
    toolgate_key = os.environ.get("PI_TOOLGATE_KEY", "").strip()
    toolgate = (
        ToolGateClient(
            os.environ.get("PI_TOOLGATE_URL", "http://toolgate-api:8010"),
            toolgate_key,
            timeout=_seconds("PI_TOOLGATE_TIMEOUT_S", 120.0),
        )
        if toolgate_key
        else None
    )

    memory_url = os.environ.get("PI_MEMORYGATE_URL", "").strip()
    ingest_key = os.environ.get("PI_MEMORYGATE_INGEST_KEY", "").strip()
    read_key = os.environ.get("PI_MEMORYGATE_READ_KEY", "").strip()
    if any((memory_url, ingest_key, read_key)) and not all((memory_url, ingest_key, read_key)):
        store.close()
        raise RuntimeError(
            "Set PI_MEMORYGATE_URL, PI_MEMORYGATE_INGEST_KEY and "
            "PI_MEMORYGATE_READ_KEY together, then restart Pi."
        )
    memory = Memory(
        store,
        MemoryClient(
            memory_url,
            ingest_key,
            read_key,
            os.environ.get("PI_MEMORYGATE_AGENT_ID", "default"),
            timeout=_seconds("PI_MEMORYGATE_TIMEOUT_S", 5.0),
        )
        if memory_url
        else None,
    )
    from . import memory_authority

    try:
        bindings = os.environ.get("PI_MEMORY_AGENT_READ_BINDINGS", "").strip()
        if bindings and not memory_url:
            raise ValueError("Configure MemoryGate before specialist memory bindings.")
        memory.read_clients = memory_authority.clients(
            bindings,
            os.environ,
            lambda namespace, key: MemoryClient(
                memory_url, "", key, namespace, timeout=_seconds("PI_MEMORYGATE_TIMEOUT_S", 5.0)
            ),
        )
    except ValueError:
        memory.close()
        store.close()
        raise
    app.state.memory = memory
    correction_values = [
        os.environ.get(name, "").strip()
        for name in (
            "PI_MEMORY_CORRECTION_URL",
            "PI_MEMORY_CORRECTION_KEY",
            "PI_MEMORY_CORRECTION_AGENT_ID",
        )
    ]
    try:
        if any(correction_values) and not all(correction_values):
            raise ValueError("Configure correction URL, key and namespace together.")
        correction = (
            memory_corrections.Client(*correction_values) if all(correction_values) else None
        )
    except ValueError:
        memory.close()
        store.close()
        raise RuntimeError(
            "Set valid PI_MEMORY_CORRECTION_URL, KEY and AGENT_ID together."
        ) from None
    app.state.memory_corrections = correction
    app.state.speech = speech_client
    app.state.speech_credential_configured = bool(
        provider_environment.get("PI_SPEECH_KEY", "").strip()
    )
    memory.start()
    app.state.admin_key = admin_key
    app.state.gateway_key_hash = runtime_hash
    app.state.owner_key_hash = owner_hash
    app.state.store = store
    app.state.local = local
    app.state.hosted = hosted
    app.state.provider_credentials = sorted(
        name
        for name, environment_name in (
            ("openrouter", "PI_OPENROUTER_KEY"),
            ("openai", "PI_OPENAI_KEY"),
            ("anthropic", "PI_ANTHROPIC_KEY"),
        )
        if provider_environment.get(environment_name, "").strip()
    )
    app.state.toolgate = toolgate
    app.state.job_executor = PublishedJobs({"companion": toolgate}) if toolgate else None
    app.state.interrupted_at_startup = interrupted
    app.state.router = Router(
        local_provider=local,
        hosted_provider=hosted,
        providers=configured_direct_providers(
            provider_environment, timeout=_seconds("PI_HOSTED_TIMEOUT_S", 180.0)
        ),
        local_model=os.environ.get("PI_MODEL", "qwen3:4b"),
    )
    from .memory_ranking import ConfiguredMemoryRanker

    memory.ranker = ConfiguredMemoryRanker(store, app.state.router.providers)
    app.state.loop = Loop(
        store,
        app.state.router,
        system_prompt=os.environ.get("PI_SYSTEM_PROMPT", ""),
        toolgate=toolgate,
        memory=memory,
    )
    scheduler = None
    if os.environ.get("PI_SCHEDULER_ENABLED", "").strip().lower() in {"1", "true", "yes"}:
        if app.state.job_executor is None:
            if correction:
                correction.close()
            memory.close()
            store.close()
            raise RuntimeError("Scheduled execution requires a scoped ToolGate credential.")
        scheduler = JobWorker(store, app.state.job_executor)
        scheduler.start()
    queue_worker = None
    if os.environ.get("PI_QUEUE_ENABLED", "").strip().lower() in {"1", "true", "yes"}:
        queue_worker = QueueWorker(store, app.state.loop)
        queue_worker.start()
    proposal_worker = None
    if os.environ.get("PI_PROPOSALS_ENABLED", "").strip().lower() in {"1", "true", "yes"}:
        from .proposal_worker import ProposalWorker

        proposal_worker = ProposalWorker(
            store,
            app.state.router,
            interval_hours=float(os.environ.get("PI_PROPOSALS_INTERVAL_HOURS", "24")),
        )
        proposal_worker.start()
    try:
        yield
    finally:
        if proposal_worker:
            proposal_worker.close()
        if queue_worker:
            queue_worker.close()
        if scheduler:
            scheduler.close()
        memory.close()
        if correction:
            correction.close()
        store.close()


app = FastAPI(title="Pi", version=SERVICE_VERSION, lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request: Request, exc: RequestValidationError):
    # FastAPI's default body includes rejected input values. Owner requests may
    # accidentally contain credentials, so validation responses are deliberately static.
    return JSONResponse({"detail": "Request validation failed."}, status_code=422)


@app.exception_handler(tasks.TaskError)
async def task_error(request: Request, exc: tasks.TaskError):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)


def require_key(
    request: Request,
    x_pi_key: str | None = Header(None, alias="X-Pi-Key"),
    gateway_key: str | None = Header(None, alias="X-Pi-Gateway-Key"),
) -> str:
    if x_pi_key and secrets.compare_digest(x_pi_key.encode(), app.state.admin_key.encode()):
        return "recovery"
    expected = getattr(app.state, "gateway_key_hash", "")
    if (
        gateway_key
        and expected
        and secrets.compare_digest(hashlib.sha256(gateway_key.encode()).hexdigest(), expected)
    ):
        if not runtime_allowed(request.method, request.url.path):
            raise HTTPException(403, "Gateway credential cannot perform this operation.")
        return "gateway-runtime"
    raise HTTPException(
        401, "Missing or invalid runtime credential. Check gateway provisioning on the host."
    )


def require_admin(identity: str = Depends(require_key)) -> None:
    if identity != "recovery":
        raise HTTPException(403, "Owner administration credential required.")


def require_owner(
    request: Request,
    owner_key: str | None = Header(None, alias="X-Pi-Owner-Key"),
    x_pi_key: str | None = Header(None, alias="X-Pi-Key"),
) -> None:
    from .browser_contract import owner_allowed

    expected = getattr(app.state, "owner_key_hash", "")
    if (
        owner_key
        and len(expected) == 64
        and secrets.compare_digest(hashlib.sha256(owner_key.encode()).hexdigest(), expected)
    ):
        if owner_allowed(request.method, request.url.path):
            return
        raise HTTPException(403, "Owner-control credential cannot perform this operation.")
    if x_pi_key and secrets.compare_digest(x_pi_key.encode(), app.state.admin_key.encode()):
        return
    raise HTTPException(401, "Missing or invalid owner-control credential.")


app.include_router(
    projects_api.router(
        lambda: app.state.store,
        require_owner,
        lambda reference: project_sources.resolve(app.state.store, reference),
    )
)
app.include_router(context_api.router(lambda: app.state.store, require_admin))
app.include_router(continuity_api.router(lambda: app.state.store, require_admin))
app.include_router(characters_api.router(lambda: app.state.store, require_owner))
app.include_router(
    system_inventory_api.router(
        lambda: app.state.store, lambda: getattr(app.state, "toolgate", None), require_owner
    )
)
app.include_router(
    filesystem_api.router(
        lambda: app.state.store, lambda: getattr(app.state, "toolgate", None), require_owner
    )
)
app.include_router(
    system_actions_api.router(
        lambda: app.state.store, lambda: getattr(app.state, "toolgate", None), require_admin
    )
)
app.include_router(
    system_actions_api.targets_router(lambda: getattr(app.state, "toolgate", None), require_admin)
)
app.include_router(
    calls_api.router(
        lambda: app.state.store,
        lambda: app.state.loop,
        require_owner,
        lambda: getattr(app.state, "speech", None),
    )
)
app.include_router(
    collaboration_api.create_router(lambda: app.state.store, require_admin, require_owner)
)
app.include_router(
    memory_proposals_api.router(
        lambda: app.state.store,
        lambda: getattr(app.state, "memory_corrections", None),
        require_admin,
    )
)
app.include_router(
    team_execution_api.create_router(lambda: app.state.store, lambda: app.state.loop, require_admin)
)
app.include_router(session_settings_api.router(lambda: app.state.store, require_owner))
app.include_router(
    artifacts_api.router(lambda: app.state.store, require_owner, session_settings.source_privacy)
)
app.include_router(model_roles_api.router(lambda: app.state.store, require_owner))
app.include_router(
    setup_status_api.router(
        lambda: app.state.store,
        lambda: len(getattr(app.state, "owner_key_hash", "")) == 64,
        lambda: app.state.memory,
        lambda: getattr(app.state, "toolgate", None),
        lambda: app.state.router,
        lambda: getattr(app.state, "speech", None),
        require_owner,
    )
)
from . import memory_explorer_api  # noqa: E402 - registered after the routers it follows

app.include_router(memory_explorer_api.router(lambda: app.state.memory, require_owner))
from . import universal_search  # noqa: E402

app.include_router(
    universal_search.router(
        lambda: app.state.store,
        lambda: app.state.memory,
        lambda: getattr(app.state, "toolgate", None),
        lambda: app.state.router.adapters(),
        require_owner,
    )
)
app.include_router(drafts_api.router(lambda: app.state.store, require_admin))
app.include_router(conversation_search.router(lambda: app.state.store, require_admin))
app.include_router(
    attachments_api.router(lambda: app.state.store, require_admin, session_settings.source_privacy)
)
app.include_router(
    model_evaluations_api.router(
        lambda: app.state.store, require_admin, lambda: app.state.router.adapters()
    )
)
app.include_router(
    jobs_api.router(
        lambda: app.state.store, require_owner, lambda: getattr(app.state, "job_executor", None)
    )
)


@app.exception_handler(context_controls.ContextError)
async def context_error(request: Request, exc: context_controls.ContextError):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)


app.include_router(owner_preferences_api.router(lambda: app.state, require_admin))


@app.exception_handler(agents.AgentError)
async def agent_error(request: Request, exc: agents.AgentError):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)


app.include_router(agents_api.router(lambda: app.state, require_owner))


def _seconds(name: str, default: float) -> float:
    """A timeout from the environment, or the default if it is not a number.

    A malformed value falls back rather than stopping the service: the owner
    typing "60s" should not take Conker offline, and the log line says what was
    used instead of what was asked for.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s is not a number (%r); using %ss", name, raw, default)
        return default
    if value <= 0:
        log.warning("%s must be positive (got %s); using %ss", name, value, default)
        return default
    return value


@app.get("/health")
def health():
    """Shape is fixed by the Conker module contract - see docs/module-contract.md."""
    now = time.monotonic()
    cached = _health_cache.get("result")
    if cached and now - _health_cache["at"] < HEALTH_CACHE_SECONDS:
        return {**cached, "age_seconds": round(now - _health_cache["at"], 1)}

    checks = {
        "store": app.state.store.health(),
        "memory": app.state.loop.memory.health(),
        "local_provider": app.state.local.health(),
        # not_configured, not unavailable: no hosted provider is a valid install,
        # not a broken one, and collapsing the two is how a dashboard starts
        # lying about what is wrong.
        "hosted_provider": (
            app.state.hosted.health()
            if app.state.hosted
            else {"status": "not_configured", "reason": "no API key"}
        ),
        "provider_credentials": {
            "status": "ok" if app.state.provider_credentials else "not_configured",
            "configured": app.state.provider_credentials,
            "verification": "host-revision-bound",
            "secrets_included": False,
        },
        "speech": {
            "status": (
                "ok"
                if any(
                    item["status"] in {"configured", "available"}
                    for item in app.state.speech.capabilities().values()
                    if isinstance(item, dict) and "status" in item
                )
                else "not_configured"
            ),
            "input": app.state.speech.capabilities()["stt"]["status"],
            "output": app.state.speech.capabilities()["tts"]["status"],
            "credentialConfigured": app.state.speech_credential_configured,
            "secrets_included": False,
        },
        "action_boundary": (
            app.state.toolgate.health()
            if app.state.toolgate
            else {"status": "not_configured", "reason": "no execution key"}
        ),
    }
    degraded = sorted(name for name, c in checks.items() if c["status"] not in HEALTHY)
    result = {
        "service": "pi",
        "version": SERVICE_VERSION,
        "status": "degraded" if degraded else "ok",
        "degraded": degraded,
        "checks": checks,
        "checked_at": datetime.now(UTC).isoformat(),
    }
    _health_cache["result"] = result
    _health_cache["at"] = now
    return {**result, "age_seconds": 0.0}


app.include_router(sessions_api.router(lambda: app.state, require_key, require_admin))


app.include_router(memory_api.router(lambda: app.state, require_key, require_owner))


app.include_router(proposals_api.router(lambda: app.state, require_key, require_owner))


@app.get("/tools", dependencies=[Depends(require_key)])
def tools():
    """What Pi may currently do, as ToolGate sees it - not as Pi remembers."""
    if app.state.toolgate is None:
        return {"status": "not_configured", "results": []}
    try:
        found = app.state.toolgate.tools()
    except Exception as exc:
        return {"status": "unavailable", "reason": type(exc).__name__, "results": []}
    return {
        "status": "ok",
        "results": [
            {"id": t.id, "name": t.name, "description": t.description, "inputs": t.inputs}
            for t in found
        ],
    }


@app.get("/models", dependencies=[Depends(require_key)])
def models():
    """What Pi can route to right now, and what each would cost.

    Discovered, never hardcoded: a static list is stale within weeks. Paid
    models are listed so the owner can see what opting in would buy, and marked
    so nothing is called by accident.
    """
    local = {
        "provider": app.state.local.name,
        "model": app.state.router.local_model,
        "free": True,
        "health": app.state.local.health(),
    }
    direct = {
        name: {
            "provider": name,
            "health": adapter.health(),
            "allow_paid": adapter.allow_paid,
            "discovery": "manual",
            "capabilities": getattr(adapter, "capabilities", ["text"]),
        }
        for name, adapter in app.state.router.providers.items()
    }
    if app.state.hosted is None:
        return {"local": local, "hosted": {"status": "not_configured"}, "direct": direct}
    try:
        catalogue = app.state.hosted.catalogue()
    except Exception as exc:
        return {
            "local": local,
            "direct": direct,
            "hosted": {"status": "unavailable", "reason": type(exc).__name__},
        }
    free = [m.id for m in app.state.hosted.free_models()]
    return {
        "local": local,
        "direct": direct,
        "hosted": {
            "status": "ok",
            "provider": app.state.hosted.name,
            "allow_paid": app.state.hosted.allow_paid,
            "free_models": free,
            "free_count": len(free),
            "total_text_models": len(catalogue),
        },
    }


app.include_router(tasks_api.router(lambda: app.state, require_key))


app.include_router(activity_api.router(lambda: app.state, require_key))


app.include_router(
    turn_queue.router(lambda: app.state.store, require_admin, lambda: app.state.loop)
)


app.include_router(message_forks.router(lambda: app.state.store, require_admin))
app.include_router(response_versions.router(lambda: app.state.store, require_admin))
app.include_router(response_retries.router(lambda: app.state.loop, require_admin))
app.include_router(turn_steering.router(lambda: app.state.store, require_admin))
