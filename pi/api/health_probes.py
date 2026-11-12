"""The health watch's probes: each part's own health signal, read in words the owner understands.

No probe calls a model, runs a search or spends anything. Reasons never carry an address, an id
or an exception text: only what broke and what that means for him.
"""

from __future__ import annotations

import httpx

from pi.models import roles as model_roles
from pi.models.loopback_http import verification
from pi.owner.health_watch import WORKS, Result, broken

PROBE_TIMEOUT_SECONDS = 5.0
# A message the outbox has tried this often without success is stuck, not just queued.
STUCK_ATTEMPTS = 5
WORKING = {"ok", "unverified", "degraded"}


def toolgate(client) -> Result:
    if client is None:
        return None
    state = client.health()
    status = state.get("status")
    # Degraded is the lockdown the owner sets on purpose: ToolGate answers and refuses.
    if status in {"ok", "degraded"}:
        return WORKS
    if status == "not_configured":
        return None
    if state.get("reason") == "execution key rejected":
        return broken(
            "Tools and actions are not working: ToolGate refuses Conker's key, so Conker "
            "cannot use tools, search the web or act."
        )
    return broken(
        "Tools and actions are not working: Conker cannot reach ToolGate, so it cannot use "
        "tools, search the web or act."
    )


def web_search(client) -> Result:
    """ToolGate's own health page says whether its search service answers; no query is sent."""
    if client is None or not getattr(client, "base_url", ""):
        return None
    try:
        response = httpx.get(
            f"{client.base_url}/health",
            verify=verification(client.base_url),
            timeout=PROBE_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        )
        response.raise_for_status()
        check = response.json()["checks"]["searxng"]["status"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None  # ToolGate itself is down or old: the ToolGate check speaks for it.
    if check == "ok":
        return WORKS
    if check == "not_configured":
        return None
    return broken("Web search is not working: the search service does not answer.")


def memorygate(memory) -> Result:
    client = getattr(memory, "client", None)
    if client is None:
        return None
    status = client.health().get("status")
    if status == "ok":
        return WORKS
    if status == "degraded":
        return broken(
            "Long-term memory is having trouble: MemoryGate reports a problem, so recall may "
            "miss things."
        )
    return broken(
        "Long-term memory is not working: Conker cannot reach MemoryGate, so chats are kept "
        "but not remembered."
    )


def memory_delivery(store, memory) -> Result:
    if getattr(memory, "client", None) is None:
        return None
    with store._connect() as db:
        blocked = db.execute("SELECT COUNT(*) FROM memory_outbox WHERE state='blocked'")
        blocked = blocked.fetchone()[0]
        stuck = db.execute(
            "SELECT COUNT(*) FROM memory_outbox WHERE state='pending' AND attempts>=?",
            (STUCK_ATTEMPTS,),
        ).fetchone()[0]
    if blocked:
        return broken(
            "Saving to memory is blocked: new chats are kept but not remembered until it is "
            "repaired."
        )
    if stuck:
        return broken(
            "Saving to memory keeps failing: Pi retries, but new chats are not remembered yet."
        )
    return WORKS


def _answer_model(configuration: dict) -> dict | None:
    """The model the answer role uses first: its own choice, else its first enabled model."""
    roles = configuration["roleSettings"]["roles"]
    assignment = roles.get("answer")
    if not assignment or not assignment["enabled"]:
        return None
    models = {model["id"]: model for model in configuration["models"] if model["enabled"]}
    for model_id in [assignment["modelId"], *assignment["eligibleModelIds"]]:
        if model_id in models:
            return models[model_id]
    return None


def answer_model(store, adapters) -> Result:
    """The answer model's provider health: reachable and set up, never a completion."""
    configuration = model_roles.load(store)["configuration"]
    model = _answer_model(configuration) if configuration else None
    if model is None:
        return None  # No answer model chosen yet: setup says so, not the watch.
    name = model["name"][:80]
    adapter = adapters().get(model["providerId"])
    if adapter is None:
        return broken(f"The answer model is not working: {name} has no provider set up.")
    if hasattr(adapter, "health_models"):
        state = adapter.health_models([model["route"]]).get(model["route"], {})
    else:
        state = adapter.health()
    if state.get("status") in WORKING:
        return WORKS
    if state.get("status") == "not_configured":
        return broken(
            f"The answer model is not working: {name} is not set up (a key, a sign-in or a "
            "download is missing)."
        )
    return broken(f"The answer model is not working: Conker cannot reach {name}'s provider.")


def probes(state) -> dict:
    """Every check, bound to the running service's parts (read again each round)."""
    memory = state.loop.memory
    return {
        "toolgate": lambda: toolgate(state.toolgate),
        "web_search": lambda: web_search(state.toolgate),
        "memorygate": lambda: memorygate(memory),
        "memory_delivery": lambda: memory_delivery(state.store, memory),
        "answer_model": lambda: answer_model(state.store, state.router.adapters),
    }
