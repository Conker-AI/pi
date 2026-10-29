"""Pi's composition root: builds the store, loop, providers, gates and workers for the app's lifetime."""

from __future__ import annotations

import hashlib
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from pi.actions.toolgate import ToolGateClient
from pi.agents import team_execution, tool_drafts
from pi.calls import runtime as calls
from pi.calls import speech
from pi.context import retrieval as context_retrieval
from pi.conversation.store import Store
from pi.host import actions as system_actions
from pi.host import filesystem_reads
from pi.host import inventory as system_inventory
from pi.memory import corrections as memory_corrections
from pi.memory import edits as memory_edits
from pi.memory import proposals as memory_proposals
from pi.memory.service import Memory, MemoryClient
from pi.models import evaluations as model_evaluations
from pi.models import secrets as provider_secrets
from pi.models.direct import configured as configured_direct_providers
from pi.models.openrouter import OpenRouterProvider
from pi.models.providers import OllamaProvider
from pi.models.routing import Router
from pi.schedule import proposals
from pi.schedule.agent_jobs import AgentJobs
from pi.schedule.job_execution import PublishedJobs
from pi.schedule.job_worker import JobWorker
from pi.turns import thinking as background_thinking
from pi.turns.loop import Loop
from pi.turns.queue_worker import QueueWorker

log = logging.getLogger("pi")


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
    background_thinking.recover_interrupted(store)
    context_retrieval.recover_interrupted(store)
    team_execution.recover_interrupted(store)
    tool_drafts.recover_interrupted(store)
    memory_proposals.recover_interrupted(store)
    memory_edits.recover_interrupted(store)
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
    from pi.memory import authority as memory_authority

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
    from pi.memory.ranking import ConfiguredMemoryRanker

    memory.ranker = ConfiguredMemoryRanker(store, app.state.router.providers)
    app.state.loop = Loop(
        store,
        app.state.router,
        system_prompt=os.environ.get("PI_SYSTEM_PROMPT", ""),
        toolgate=toolgate,
        memory=memory,
    )
    app.state.loop.remember = correction.remember if correction is not None else None
    if app.state.job_executor is not None:
        app.state.job_executor = AgentJobs(store, app.state.loop, app.state.job_executor)
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
        from pi.schedule.proposal_worker import ProposalWorker

        proposal_worker = ProposalWorker(
            store,
            app.state.router,
            interval_hours=float(os.environ.get("PI_PROPOSALS_INTERVAL_HOURS", "24")),
        )
        proposal_worker.start()
    plan_worker = None

    def plan_readers():
        adapters = app.state.router.adapters().values()
        return [adapter.plan_limits for adapter in adapters if hasattr(adapter, "plan_limits")]

    # Subscription runtimes report plan usage; the owner's planAlerts preference turns checks off.
    if plan_readers():
        from pi.owner.plan_alerts import PlanAlertWorker

        plan_worker = PlanAlertWorker(
            store, plan_readers, check_seconds=_seconds("PI_PLAN_CHECK_SECONDS", 900)
        )
        plan_worker.start()
    # Conker's tool requests are drafted in the background, one at a time.
    app.state.loop.drafts.start()
    try:
        yield
    finally:
        app.state.loop.drafts.close()
        app.state.loop.thinking.close()
        if plan_worker:
            plan_worker.close()
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
