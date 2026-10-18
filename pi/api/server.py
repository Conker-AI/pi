"""Pi's HTTP surface.

The worker API behind the authenticated browser gateway. Every route is Pi's own state -
sessions, messages, turns - because Pi coordinates and never owns anything else:
memory belongs to MemoryGate, actions to ToolGate, machine truth to SystemGate.
Tool calls arrive in #29 and go out through ToolGate, never from here.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from datetime import UTC, datetime

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from pi.agents import profiles as agents
from pi.api.browser_contract import runtime_allowed
from pi.api.routes import activity as activity_api
from pi.api.routes import agents as agents_api
from pi.api.routes import artifacts as artifacts_api
from pi.api.routes import attachments as attachments_api
from pi.api.routes import auto_settings as auto_settings_api
from pi.api.routes import calls as calls_api
from pi.api.routes import characters as characters_api
from pi.api.routes import context as context_api
from pi.api.routes import continuity as continuity_api
from pi.api.routes import decisions as decisions_api
from pi.api.routes import drafts as drafts_api
from pi.api.routes import filesystem as filesystem_api
from pi.api.routes import jobs as jobs_api
from pi.api.routes import memory as memory_api
from pi.api.routes import memory_edits as memory_edits_api
from pi.api.routes import memory_proposals as memory_proposals_api
from pi.api.routes import model_evaluations as model_evaluations_api
from pi.api.routes import model_profiles as model_profiles_api
from pi.api.routes import model_roles as model_roles_api
from pi.api.routes import naming as naming_api
from pi.api.routes import output_settings as output_settings_api
from pi.api.routes import owner_preferences as owner_preferences_api
from pi.api.routes import projects as projects_api
from pi.api.routes import proposals as proposals_api
from pi.api.routes import session_settings as session_settings_api
from pi.api.routes import sessions as sessions_api
from pi.api.routes import setup_status as setup_status_api
from pi.api.routes import skills as skills_api
from pi.api.routes import summary_draft as summary_draft_api
from pi.api.routes import system_actions as system_actions_api
from pi.api.routes import system_inventory as system_inventory_api
from pi.api.routes import tasks as tasks_api
from pi.api.routes import team_execution as team_execution_api
from pi.api.routes import teams as collaboration_api
from pi.api.startup import lifespan
from pi.context import controls as context_controls
from pi.context import project_sources
from pi.conversation import fact_checks, judgements
from pi.conversation import forks as message_forks
from pi.conversation import retries as response_retries
from pi.conversation import search as conversation_search
from pi.conversation import settings as session_settings
from pi.conversation import versions as response_versions
from pi.schedule import tasks
from pi.turns import queue as turn_queue
from pi.turns import steering as turn_steering

SERVICE_VERSION = "0.5.6"
HEALTHY = {"ok", "not_configured"}
HEALTH_CACHE_SECONDS = 5.0

_health_cache: dict = {}


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
    from pi.api.browser_contract import owner_allowed

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


def require_skill_read(
    request: Request,
    owner_key: str | None = Header(None, alias="X-Pi-Owner-Key"),
    x_pi_key: str | None = Header(None, alias="X-Pi-Key"),
    gateway_key: str | None = Header(None, alias="X-Pi-Gateway-Key"),
) -> None:
    if owner_key:
        require_owner(request, owner_key, x_pi_key)
    else:
        require_key(request, x_pi_key, gateway_key)


app.include_router(skills_api.router(lambda: app.state.store, require_skill_read, require_owner))
app.include_router(context_api.router(lambda: app.state.store, require_owner))
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
    memory_edits_api.router(
        lambda: app.state.store,
        lambda: getattr(app.state, "memory_corrections", None),
        lambda: getattr(getattr(app.state, "memory", None), "client", None),
        require_owner,
    )
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
app.include_router(output_settings_api.router(lambda: app.state.store, require_admin))
app.include_router(decisions_api.router(lambda: app.state.store, require_admin))
app.include_router(
    naming_api.router(lambda: app.state.store, lambda: app.state.router.adapters(), require_admin)
)
app.include_router(
    artifacts_api.router(lambda: app.state.store, require_owner, session_settings.source_privacy)
)
app.include_router(model_roles_api.router(lambda: app.state.store, require_owner))
app.include_router(model_profiles_api.router(lambda: app.state.store, require_admin))
app.include_router(
    auto_settings_api.router(
        lambda: app.state.store, lambda: app.state.router.adapters(), require_key
    )
)
app.include_router(
    summary_draft_api.router(
        lambda: app.state.store, lambda: app.state.router.adapters(), require_admin
    )
)
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
from pi.api.routes import memory_explorer as memory_explorer_api  # noqa: E402

app.include_router(memory_explorer_api.router(lambda: app.state.memory, require_owner))
from pi.context import universal_search  # noqa: E402

app.include_router(
    universal_search.router(
        lambda: app.state.store,
        lambda: app.state.memory,
        lambda: getattr(app.state, "toolgate", None),
        lambda: app.state.router.adapters(),
        require_owner,
    )
)
app.include_router(drafts_api.router(lambda: app.state.store, require_owner))
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


@app.get("/health")
def health():
    """Shape is fixed by the Conker module contract - see docs/module-contract.md."""
    now = time.monotonic()
    cached = _health_cache.get("result")
    if cached and now - _health_cache["at"] < HEALTH_CACHE_SECONDS:
        return {**cached, "age_seconds": round(now - _health_cache["at"], 1)}

    hosted_health = (
        app.state.hosted.health()
        if app.state.hosted
        else {"status": "not_configured", "reason": "no API key"}
    )
    if hosted_health["status"] == "unverified":
        # Provider readiness has a separate vocabulary from module health.
        hosted_health = {"status": "unknown", "reason": "Configured; inference not verified"}
    checks = {
        "store": app.state.store.health(),
        "memory": app.state.loop.memory.health(),
        "local_provider": app.state.local.health(),
        # not_configured, not unavailable: no hosted provider is a valid install,
        # not a broken one, and collapsing the two is how a dashboard starts
        # lying about what is wrong.
        "hosted_provider": hosted_health,
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
            "hosted": {
                "status": "unavailable",
                "provider": app.state.hosted.name,
                "allow_paid": app.state.hosted.allow_paid,
                "reason": type(exc).__name__,
            },
        }
    free = [
        model.id
        for model in sorted(
            (model for model in catalogue.values() if model.is_free),
            key=lambda model: -model.context_length,
        )
    ]
    return {
        "local": local,
        "direct": direct,
        "hosted": {
            "status": "unverified",
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
app.include_router(
    judgements.router(lambda: app.state.store, lambda: app.state.router.adapters(), require_admin)
)
app.include_router(
    fact_checks.router(lambda: app.state.store, lambda: app.state.router.adapters(), require_admin)
)
app.include_router(turn_steering.router(lambda: app.state.store, require_admin))
