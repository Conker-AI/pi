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
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from . import activity, agents, artifacts_api, collaboration_api, context_api, context_controls, model_roles_api, owner_preferences, projects_api, session_settings, session_settings_api, submissions, tasks
from .browser_contract import runtime_allowed
from . import jobs_api, turn_control, turn_queue, message_forks, response_versions, response_retries, turn_steering
from .job_execution import PublishedJobs
from .job_worker import JobWorker
from .queue_worker import QueueWorker
from . import project_sources
from . import drafts_api
from . import conversation_search
from . import attachments_api
from . import model_evaluations, model_evaluations_api
from . import context_retrieval
from . import team_execution, team_execution_api
from . import memory_corrections, memory_proposals, memory_proposals_api
from . import continuity_api
from . import calls, calls_api, speech
from . import characters_api
from . import system_inventory, system_inventory_api
from . import filesystem_api, filesystem_reads
from . import system_actions, system_actions_api
from .direct_providers import configured as configured_direct_providers
from .loop import ActedWithoutReply, Loop, TurnFailed
from .memory import Memory, MemoryClient
from .openrouter import OpenRouterProvider
from .providers import OllamaProvider
from .routing import Router
from .store import Store
from .toolgate import ToolGateClient

log = logging.getLogger("pi")

SERVICE_VERSION = "0.4.0"
HEALTHY = {"ok", "not_configured"}
HEALTH_CACHE_SECONDS = 5.0

_health_cache: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    admin_key = os.environ.get("PI_ADMIN_KEY", "").strip()
    runtime_hash = os.environ.get("PI_GATEWAY_KEY_SHA256", "").strip()
    owner_hash = os.environ.get("PI_OWNER_KEY_SHA256", "").strip()
    if owner_hash and (len(owner_hash) != 64 or any(c not in "0123456789abcdef" for c in owner_hash)
                       or owner_hash in {runtime_hash, hashlib.sha256(admin_key.encode()).hexdigest()}):
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
    speech_client = speech.SpeechClient(
        url=os.environ.get("PI_SPEECH_URL", "").strip(),
        key=os.environ.get("PI_SPEECH_KEY", "").strip(),
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
    openrouter_key = os.environ.get("PI_OPENROUTER_KEY", "").strip()
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
    toolgate = ToolGateClient(
        os.environ.get("PI_TOOLGATE_URL", "http://toolgate-api:8010"), toolgate_key,
        timeout=_seconds("PI_TOOLGATE_TIMEOUT_S", 120.0),
    ) if toolgate_key else None

    memory_url = os.environ.get("PI_MEMORYGATE_URL", "").strip()
    ingest_key = os.environ.get("PI_MEMORYGATE_INGEST_KEY", "").strip()
    read_key = os.environ.get("PI_MEMORYGATE_READ_KEY", "").strip()
    if any((memory_url, ingest_key, read_key)) and not all((memory_url, ingest_key, read_key)):
        store.close()
        raise RuntimeError("Set PI_MEMORYGATE_URL, PI_MEMORYGATE_INGEST_KEY and "
                           "PI_MEMORYGATE_READ_KEY together, then restart Pi.")
    memory = Memory(store, MemoryClient(memory_url, ingest_key, read_key,
        os.environ.get("PI_MEMORYGATE_AGENT_ID", "default"),
        timeout=_seconds("PI_MEMORYGATE_TIMEOUT_S", 5.0)) if memory_url else None)
    from . import memory_authority
    try:
        bindings = os.environ.get("PI_MEMORY_AGENT_READ_BINDINGS", "").strip()
        if bindings and not memory_url:
            raise ValueError("Configure MemoryGate before specialist memory bindings.")
        memory.read_clients = memory_authority.clients(bindings, os.environ,
            lambda namespace, key: MemoryClient(memory_url, "", key, namespace,
                timeout=_seconds("PI_MEMORYGATE_TIMEOUT_S", 5.0)))
    except ValueError:
        memory.close()
        store.close()
        raise
    app.state.memory = memory
    correction_values = [os.environ.get(name, '').strip() for name in (
        'PI_MEMORY_CORRECTION_URL', 'PI_MEMORY_CORRECTION_KEY', 'PI_MEMORY_CORRECTION_AGENT_ID')]
    try:
        if any(correction_values) and not all(correction_values):
            raise ValueError('Configure correction URL, key and namespace together.')
        correction = memory_corrections.Client(*correction_values) if all(correction_values) else None
    except ValueError:
        memory.close()
        store.close()
        raise RuntimeError('Set valid PI_MEMORY_CORRECTION_URL, KEY and AGENT_ID together.') from None
    app.state.memory_corrections = correction
    app.state.speech = speech_client
    memory.start()
    app.state.admin_key = admin_key
    app.state.gateway_key_hash = runtime_hash
    app.state.owner_key_hash = owner_hash
    app.state.store = store
    app.state.local = local
    app.state.hosted = hosted
    app.state.toolgate = toolgate
    app.state.job_executor = PublishedJobs({"companion": toolgate}) if toolgate else None
    app.state.interrupted_at_startup = interrupted
    app.state.router = Router(
        local_provider=local, hosted_provider=hosted,
        providers=configured_direct_providers(
            os.environ, timeout=_seconds("PI_HOSTED_TIMEOUT_S", 180.0)),
        local_model=os.environ.get("PI_MODEL", "qwen3:4b"),
    )
    from .memory_ranking import ConfiguredMemoryRanker
    memory.ranker = ConfiguredMemoryRanker(store, app.state.router.providers)
    app.state.loop = Loop(
        store, app.state.router,
        system_prompt=os.environ.get("PI_SYSTEM_PROMPT", ""),
        toolgate=toolgate, memory=memory,
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
    try:
        yield
    finally:
        if queue_worker:
            queue_worker.close()
        if scheduler:
            scheduler.close()
        memory.close()
        if correction:
            correction.close()
        store.close()


app = FastAPI(title="Pi", version=SERVICE_VERSION, lifespan=lifespan)


@app.exception_handler(tasks.TaskError)
async def task_error(request: Request, exc: tasks.TaskError):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)


def require_key(request: Request, x_pi_key: str | None = Header(None, alias="X-Pi-Key"),
                gateway_key: str | None = Header(None, alias="X-Pi-Gateway-Key")) -> str:
    if x_pi_key and secrets.compare_digest(x_pi_key.encode(), app.state.admin_key.encode()):
        return "recovery"
    expected = getattr(app.state, "gateway_key_hash", "")
    if (gateway_key and expected and secrets.compare_digest(
            hashlib.sha256(gateway_key.encode()).hexdigest(), expected)):
        if not runtime_allowed(request.method, request.url.path):
            raise HTTPException(403, "Gateway credential cannot perform this operation.")
        return "gateway-runtime"
    raise HTTPException(
        401, "Missing or invalid runtime credential. Check gateway provisioning on the host."
    )


def require_admin(identity: str = Depends(require_key)) -> None:
    if identity != "recovery":
        raise HTTPException(403, "Owner administration credential required.")


def require_owner(request: Request, owner_key: str | None = Header(None, alias="X-Pi-Owner-Key"),
                  x_pi_key: str | None = Header(None, alias="X-Pi-Key")) -> None:
    from .browser_contract import owner_allowed
    expected = getattr(app.state, "owner_key_hash", "")
    if (owner_key and len(expected) == 64 and secrets.compare_digest(
            hashlib.sha256(owner_key.encode()).hexdigest(), expected)):
        if owner_allowed(request.method, request.url.path):
            return
        raise HTTPException(403, "Owner-control credential cannot perform this operation.")
    if x_pi_key and secrets.compare_digest(x_pi_key.encode(), app.state.admin_key.encode()):
        return
    raise HTTPException(401, "Missing or invalid owner-control credential.")


app.include_router(projects_api.router(lambda: app.state.store, require_admin,
    lambda reference: project_sources.resolve(app.state.store, reference)))
app.include_router(context_api.router(lambda: app.state.store, require_admin))
app.include_router(continuity_api.router(lambda: app.state.store, require_admin))
app.include_router(characters_api.router(lambda: app.state.store, require_admin))
app.include_router(system_inventory_api.router(lambda: app.state.store,
    lambda: getattr(app.state, "toolgate", None), require_admin))
app.include_router(filesystem_api.router(lambda: app.state.store,
    lambda: getattr(app.state, "toolgate", None), require_admin))
app.include_router(system_actions_api.router(lambda: app.state.store,
    lambda: getattr(app.state, "toolgate", None), require_admin))
app.include_router(system_actions_api.targets_router(
    lambda: getattr(app.state, "toolgate", None), require_admin))
app.include_router(calls_api.router(lambda: app.state.store, lambda: app.state.loop,
    require_admin, lambda: getattr(app.state, "speech", None)))
app.include_router(collaboration_api.create_router(lambda: app.state.store, require_admin))
app.include_router(memory_proposals_api.router(lambda: app.state.store,
    lambda: getattr(app.state, 'memory_corrections', None), require_admin))
app.include_router(team_execution_api.create_router(
    lambda: app.state.store, lambda: app.state.loop, require_admin))
app.include_router(session_settings_api.router(lambda: app.state.store, require_owner))
app.include_router(artifacts_api.router(lambda: app.state.store, require_admin, session_settings.source_privacy))
app.include_router(model_roles_api.router(lambda: app.state.store, require_owner))
from . import memory_explorer_api
app.include_router(memory_explorer_api.router(lambda: app.state.memory, require_owner))
app.include_router(drafts_api.router(lambda: app.state.store, require_admin))
app.include_router(conversation_search.router(lambda: app.state.store, require_admin))
app.include_router(attachments_api.router(lambda: app.state.store, require_admin,
                                         session_settings.source_privacy))
app.include_router(model_evaluations_api.router(lambda: app.state.store, require_admin,
    lambda: app.state.router.adapters()))
app.include_router(jobs_api.router(lambda: app.state.store, require_admin,
                                  lambda: getattr(app.state, "job_executor", None)))


@app.exception_handler(context_controls.ContextError)
async def context_error(request: Request, exc: context_controls.ContextError):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)


@app.get("/owner/preferences", dependencies=[Depends(require_admin)])
def get_owner_preferences():
    return owner_preferences.load(app.state.store)


@app.post("/owner/preferences", dependencies=[Depends(require_admin)])
def save_owner_preferences(body: owner_preferences.UpdatePreferences):
    try:
        return owner_preferences.save(app.state.store, body)
    except owner_preferences.RevisionConflict as exc:
        raise HTTPException(409, {"code": "revision_conflict", "message": str(exc),
                                  "current_revision": exc.current_revision}) from exc


@app.exception_handler(agents.AgentError)
async def agent_error(request: Request, exc: agents.AgentError):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)


@app.get("/agents", dependencies=[Depends(require_admin)])
def list_agents():
    return agents.list_agents(app.state.store)


@app.post("/agents", dependencies=[Depends(require_admin)])
def create_agent(body: agents.AgentInput):
    return agents.create(app.state.store, body)


@app.get("/agents/{identity}", dependencies=[Depends(require_admin)])
def get_agent(identity: str):
    return agents.get(app.state.store, identity)


@app.get("/agents/{identity}/versions", dependencies=[Depends(require_admin)])
def agent_history(identity: str):
    return agents.history(app.state.store, identity)


@app.get("/agents/{identity}/versions/{revision}", dependencies=[Depends(require_admin)])
def agent_version(identity: str, revision: int):
    return agents.get(app.state.store, identity, revision)


@app.post("/agents/{identity}/update", dependencies=[Depends(require_admin)])
def update_agent(identity: str, body: agents.UpdateAgent):
    return agents.update(app.state.store, identity, body)


@app.post("/agents/{identity}/archive", dependencies=[Depends(require_admin)])
def archive_agent(identity: str, body: agents.ArchiveAgent):
    return agents.archive(app.state.store, identity, body)


class NewSession(BaseModel):
    title: str = ""


class TurnRequest(BaseModel):
    text: str = Field(min_length=1, max_length=16000,
                      description="Send at most 16000 characters per message; split longer text.")
    # Routing hints the caller genuinely knows. The router is deliberately not
    # a classifier that reads the message - that would be a model nobody
    # evaluates deciding how much every turn costs.
    needs_tools: bool = False
    is_analysis: bool = False
    owner_requested_strong: bool = False
    request_id: str | None = Field(default=None, min_length=16, max_length=128,
                                  pattern=r"^[A-Za-z0-9_-]+$")
    task_id: str | None = Field(default=None, min_length=1, max_length=128)
    task_expected_revision: int | None = Field(default=None, ge=1, strict=True)
    draft_revision: int | None = Field(default=None, ge=1, strict=True)
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)
    model_id: str | None = Field(default=None, min_length=1, max_length=200)
    reply_to: str | None = Field(default=None, min_length=1, max_length=200)
    research_mode: Literal["off", "web", "deep"] = "off"

    @model_validator(mode="after")
    def task_submission(self):
        if (self.task_id is None) != (self.task_expected_revision is None):
            raise ValueError("Task identity and revision belong together.")
        if self.task_id is not None and self.request_id is None:
            raise ValueError("Task-bound submissions require a retained request identity.")
        return self


class ResumeRequest(BaseModel):
    job_id: str | None = Field(default=None, min_length=1, max_length=100)


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


def _acted_without_reply(exc: ActedWithoutReply) -> dict:
    """The one answer that is true when a tool ran and the model then did not.

    Not an error status, and not `complete` either. The action happened, it is
    recorded, and the caller is told plainly that the reply is what is missing
    and where to ask for it again.
    """
    return {"turn_id": exc.turn_id, "status": "acted_no_reply", "acted": True,
            "memory": app.state.loop.memory.status(
                app.state.store.get_turn(exc.turn_id)["session_id"], exc.turn_id),
            "message": None, "detail": exc.cause,
            "hint": f"the action ran and was recorded; POST /turns/{exc.turn_id}/resume "
                    "to ask for the reply again - it will not run the action a second time"}


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
        "hosted_provider": (app.state.hosted.health() if app.state.hosted
                            else {"status": "not_configured", "reason": "no API key"}),
        "action_boundary": (app.state.toolgate.health() if app.state.toolgate
                            else {"status": "not_configured", "reason": "no execution key"}),
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


@app.post("/sessions", dependencies=[Depends(require_key)])
def create_session(body: NewSession):
    return {"session_id": app.state.store.create_session(title=body.title)}


@app.get("/sessions", dependencies=[Depends(require_key)])
def list_sessions(limit: int = 50):
    return {"results": app.state.store.list_sessions(limit=min(max(limit, 1), 200))}


@app.get("/sessions/{session_id}", dependencies=[Depends(require_key)])
def get_session(session_id: str):
    session = app.state.store.get_session(session_id)
    if session is None:
        raise HTTPException(404, "no such session")
    pending = submissions.list_pending(app.state.store, session_id, limit=100)
    return {**session,
            "pending_submissions": pending["results"],
            "pending_submissions_truncated": pending["next_cursor"] is not None,
            "messages": app.state.store.messages(session_id),
            "turns": [{**turn, "memory": app.state.loop.memory.status(session_id, turn["id"])}
                      for turn in app.state.store.turns(session_id)],
            "memory": app.state.loop.memory.status(session_id)}


@app.post("/sessions/{session_id}/turns", dependencies=[Depends(require_key)])
def run_turn(session_id: str, body: TurnRequest):
    try:
        return app.state.loop.run_turn(session_id, body.text, context={
            "needs_tools": body.needs_tools,
            "is_analysis": body.is_analysis,
            "owner_requested_strong": body.owner_requested_strong,
        }, request_id=body.request_id, task_id=body.task_id,
            task_expected_revision=body.task_expected_revision,
            **({"draft_revision": body.draft_revision} if body.draft_revision is not None else {}),
            **({"attachment_ids": body.attachment_ids} if body.attachment_ids else {}),
            **({"model_id": body.model_id} if body.model_id is not None else {}),
            **({"reply_to": body.reply_to} if body.reply_to is not None else {}),
            **({"research_mode": body.research_mode} if body.research_mode != "off" else {}))
    except ActedWithoutReply as exc:
        # Deliberately not an error status. A tool ran, so this request did the
        # thing that actually matters, and the one detail missing is what the
        # model would have said about it. An error code would invite a retry,
        # and retrying this turn would run the action a second time.
        result = _acted_without_reply(exc)
        if body.request_id:
            result["submission"] = submissions.get(app.state.store, body.request_id)
        return result
    except TurnFailed as exc:
        turn = app.state.store.get_turn(exc.turn_id) if exc.turn_id else None
        if turn and turn["status"] == "cancelled":
            result = {"turn_id": exc.turn_id, "session_id": turn["session_id"],
                      "status": "cancelled", "acted": bool(turn["acted"]), "message": None}
            if body.request_id:
                result["submission"] = submissions.get(app.state.store, body.request_id)
            return result
        # 503, not 500: the provider did not answer, which is a state the caller
        # can act on. The user's message is already stored either way.
        raise HTTPException(503, {"message": f"turn failed: {exc.reason}",
                                  "turn_id": exc.turn_id,
                                  "memory": app.state.loop.memory.status(
                                      session_id, exc.turn_id),
                                  "request_id": body.request_id}) from exc


@app.get("/turn-submissions/{request_id}", dependencies=[Depends(require_key)])
def get_submission(request_id: str):
    return submissions.get(app.state.store, request_id)


@app.get("/turns/{turn_id}/research", dependencies=[Depends(require_key)])
def get_research_receipt(turn_id: str):
    from . import research
    return research.receipt(app.state.store, turn_id)


@app.get("/sessions/{session_id}/submissions", dependencies=[Depends(require_key)])
def list_pending_submissions(session_id: str, limit: int = Query(default=50, ge=1, le=200),
                             cursor: str | None = Query(default=None, max_length=128)):
    return submissions.list_pending(app.state.store, session_id, limit, cursor)


@app.get("/turns/unreplied", dependencies=[Depends(require_key)])
def unreplied():
    """Turns that acted but never reported back.

    Resumable, and resuming asks only for the missing reply - the action is
    never repeated. Separate from /approvals on purpose: these need a retry,
    not a decision.
    """
    return {"results": app.state.store.acted_without_reply()}


@app.get("/approvals", dependencies=[Depends(require_key)])
def approvals():
    """Every turn parked on the owner, across all sessions.

    One queue rather than a per-session hunt: an approval the owner never sees
    is an action that silently never happens.
    """
    return {"results": app.state.store.awaiting_approval()}


@app.post("/turns/{turn_id}/resume", dependencies=[Depends(require_key)])
def resume(turn_id: str, body: ResumeRequest | None = None):
    """Continue a parked turn after the owner approved it in ToolGate.

    Pi does not grant approvals and does not hold them. This replays the exact
    stored action; ToolGate consumes the nonce, once, and refuses a replay.
    """
    try:
        return app.state.loop.resume_turn(turn_id, job_id=body.job_id if body else None)
    except ActedWithoutReply as exc:
        return _acted_without_reply(exc)
    except TurnFailed as exc:
        raise HTTPException(409, {"message": f"cannot resume: {exc.reason}",
                                  "memory": app.state.loop.memory.status(turn_id=turn_id)}) from exc


@app.get("/tools", dependencies=[Depends(require_key)])
def tools():
    """What Pi may currently do, as ToolGate sees it - not as Pi remembers."""
    if app.state.toolgate is None:
        return {"status": "not_configured", "results": []}
    try:
        found = app.state.toolgate.tools()
    except Exception as exc:
        return {"status": "unavailable", "reason": type(exc).__name__, "results": []}
    return {"status": "ok", "results": [
        {"id": t.id, "name": t.name, "description": t.description, "inputs": t.inputs}
        for t in found
    ]}


@app.get("/models", dependencies=[Depends(require_key)])
def models():
    """What Pi can route to right now, and what each would cost.

    Discovered, never hardcoded: a static list is stale within weeks. Paid
    models are listed so the owner can see what opting in would buy, and marked
    so nothing is called by accident.
    """
    local = {"provider": app.state.local.name, "model": app.state.router.local_model,
             "free": True, "health": app.state.local.health()}
    direct = {name: {"provider": name, "health": adapter.health(),
                     "allow_paid": adapter.allow_paid, "discovery": "manual",
                     "capabilities": getattr(adapter, "capabilities", ["text"])}
              for name, adapter in app.state.router.providers.items()}
    if app.state.hosted is None:
        return {"local": local, "hosted": {"status": "not_configured"}, "direct": direct}
    try:
        catalogue = app.state.hosted.catalogue()
    except Exception as exc:
        return {"local": local, "direct": direct,
                "hosted": {"status": "unavailable", "reason": type(exc).__name__}}
    free = [m.id for m in app.state.hosted.free_models()]
    return {"local": local, "direct": direct, "hosted": {
        "status": "ok", "provider": app.state.hosted.name,
        "allow_paid": app.state.hosted.allow_paid,
        "free_models": free, "free_count": len(free), "total_text_models": len(catalogue),
    }}


@app.post("/sessions/{session_id}/fork", dependencies=[Depends(require_key)])
def fork_session(session_id: str):
    if app.state.store.get_session(session_id) is None:
        raise HTTPException(404, "no such session")
    try:
        return {"session_id": app.state.loop.fork(session_id), "parent_id": session_id}
    except TurnFailed as exc:
        raise HTTPException(409, exc.reason) from exc


@app.get("/messages/{message_id}", dependencies=[Depends(require_key)])
def get_message(message_id: str):
    message = app.state.store.get_message(message_id)
    if message is None:
        raise HTTPException(404, "no such message")
    return message


@app.get("/memory", dependencies=[Depends(require_key)])
def memory_status():
    """Delivery progress is server state; a model cannot hide this notice."""
    return app.state.loop.memory.status()


@app.get("/tasks", dependencies=[Depends(require_key)])
def list_tasks(limit: int = Query(default=50, ge=1, le=200),
               cursor: str | None = Query(default=None, max_length=128),
               session_id: str | None = Query(default=None, max_length=128)):
    return tasks.list_tasks(app.state.store, limit, cursor, session_id)


@app.post("/tasks", dependencies=[Depends(require_key)])
def create_task(body: tasks.CreateTask):
    return tasks.create(app.state.store, body)


@app.get("/tasks/requests/{request_id}", dependencies=[Depends(require_key)])
def task_by_request(request_id: str):
    return tasks.by_request(app.state.store, request_id)


@app.get("/tasks/{task_id}", dependencies=[Depends(require_key)])
def get_task(task_id: str):
    return tasks.get(app.state.store, task_id)


@app.post("/tasks/{task_id}/update", dependencies=[Depends(require_key)])
def update_task(task_id: str, body: tasks.UpdateTask):
    return tasks.update(app.state.store, task_id, body)


@app.post("/tasks/{task_id}/transition", dependencies=[Depends(require_key)])
def transition_task(task_id: str, body: tasks.TransitionTask):
    return tasks.transition(app.state.store, task_id, body)


@app.post("/tasks/{task_id}/archive", dependencies=[Depends(require_key)])
def archive_task(task_id: str, body: tasks.ArchiveTask):
    return tasks.archive(app.state.store, task_id, body)


@app.get("/runs", dependencies=[Depends(require_key)])
def list_runs(limit: int = Query(default=50, ge=1, le=200),
              cursor: str | None = Query(default=None, max_length=128),
              session_id: str | None = Query(default=None, max_length=128),
              task_id: str | None = Query(default=None, max_length=128)):
    return activity.list_runs(app.state.store, limit, cursor, session_id, task_id)


@app.get("/runs/{run_id}", dependencies=[Depends(require_key)])
def get_run(run_id: str):
    return activity.get_run(app.state.store, run_id)


@app.get("/events", dependencies=[Depends(require_key)])
def list_events(limit: int = Query(default=50, ge=1, le=200),
                cursor: str | None = Query(default=None, max_length=128),
                session_id: str | None = Query(default=None, max_length=128),
                task_id: str | None = Query(default=None, max_length=128),
                run_id: str | None = Query(default=None, max_length=128)):
    return activity.list_events(app.state.store, limit, cursor, session_id, task_id, run_id)


@app.post("/turns/{turn_id}/cancel", dependencies=[Depends(require_admin)])
def cancel_turn(turn_id: str):
    return turn_control.cancel(app.state.store, turn_id)


@app.post("/turn-submissions/{request_id}/cancel", dependencies=[Depends(require_admin)])
def cancel_submission(request_id: str):
    return turn_control.cancel_submission(app.state.store, request_id)


class ReplyRecoveryRequest(BaseModel):
    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


@app.post("/turns/{turn_id}/reply-only", dependencies=[Depends(require_admin)])
def recover_turn_reply(turn_id: str, body: ReplyRecoveryRequest):
    try:
        return app.state.loop.recover_reply(turn_id, body.request_id)
    except ActedWithoutReply as exc:
        return {**_acted_without_reply(exc), "request_id": body.request_id}


app.include_router(turn_queue.router(lambda: app.state.store, require_admin, lambda: app.state.loop))


app.include_router(message_forks.router(lambda: app.state.store, require_admin))
app.include_router(response_versions.router(lambda: app.state.store, require_admin))
app.include_router(response_retries.router(lambda: app.state.loop, require_admin))
app.include_router(turn_steering.router(lambda: app.state.store, require_admin))
