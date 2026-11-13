"""Conker's requests to make an agent or a team (`conker.create_agent`, `conker.create_team`).

Each files a change request (kinds `agent` and `team`, `pi.agents.change_requests`) with the exact
thing to make; nothing exists until the owner approves it in the Inbox, or his chat or agent
settings let Conker approve it at once (Keep, `by="conker"`). A durable agent or team carries
instructions the model wrote, so his own typed words never approve it on their own
(`pi.turns.access`). Approving makes it in the same transaction as the decision, through the same
functions his own screens use (`profiles.create`, `teams.save`).

Guards, so a new agent never gets more than he allowed:
- Tools: only ids the calling agent can use this turn, checked when the request is filed: what
  ToolGate lists for Pi's key (narrowed to the caller's own `toolIds` for an agent that is not the
  companion) plus Conker's own tools the caller is offered. An unknown id is refused then, since
  agent profiles do no reference checking of their own. Conker's create and delegation tools
  (`NEVER_GIVEN`) are always left out: a made agent never makes agents or teams, or calls agents.
- A new agent uses the default model, no per-tool access (`toolAccess` empty: the tools' own
  defaults; he loosens them himself) and reads only the chat it is in (memory `conversation`).
  These are checked again at approval, so no stored request can carry more.
- A team is linear (role 1 hands its result to role 2, and so on), 2 to 6 roles, each on an
  existing, active agent with that agent's tools and no memory of its own. Its budget is small:
  each role one turn, `ROLE_TOKENS` tokens and `ROLE_CENTS` cents; the team `roles + 2` turns and
  the roles' sums. A zero cost limit would stop the first model call, so the cap is small, not
  zero. Running a team stays his: each handoff waits for his review.
- Names are unique (archived ones count) when filed and again when approved.

Undo is archiving, from his Agents and Teams screens. Forgetting the chat archives what it made on
its own (`pi.agents.keep`).
"""

from __future__ import annotations

import json
import time

from pydantic import ValidationError

from pi.agents import change_requests, teams
from pi.agents import profiles as agents
from pi.agents.team_models import Team
from pi.conversation import visibility

NEVER_GIVEN = frozenset({"conker.create_agent", "conker.create_team", "conker.run_agent"})
MIN_ROLES, MAX_ROLES = 2, 6
MAX_TOOLS = 100
ROLE_TOKENS = 50_000
ROLE_CENTS = 25


class Refused(ValueError):
    """Not filed, in words Conker passes on."""


def _text(args, name, limit) -> str:
    value = args.get(name) if isinstance(args, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Give {name} as text.")
    return value.strip()[:limit]


def visible(loop, execution, own) -> set[str]:
    """Tool ids the calling agent can use this turn: ToolGate's for Pi's key, narrowed to an
    agent's own selection, plus `own` (Conker's tools it is offered)."""
    available = getattr(loop, "_available_tools", None)
    ids = {tool.id for tool in (available(execution) if available else [])} | set(own)
    if execution and execution.get("kind") not in (None, "companion"):
        ids &= set(execution["configuration"]["toolIds"])
    return ids


def _tools(value, allowed) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError("Give toolIds as a list of tool ids.")
    ids = [v for v in dict.fromkeys(v.strip() for v in value) if v not in NEVER_GIVEN]
    unknown = [v for v in ids if v not in allowed]
    if unknown:
        raise Refused(
            "A new agent can only get tools you can use in this turn; not filed. Not yours to "
            f"give: {', '.join(unknown[:5])}."
        )
    if len(ids) > MAX_TOOLS:
        raise Refused(f"A new agent gets at most {MAX_TOOLS} tools; not filed.")
    return ids


def _name_free(db, kind, name) -> None:
    taken = (
        db.execute("SELECT 1 FROM agents WHERE name_key=?", (name.casefold(),)).fetchone()
        if kind == "agent"
        else db.execute(
            "SELECT 1 FROM collaboration_records WHERE kind='team' AND name_key=? "
            "AND deleted_at IS NULL",
            (name.casefold(),),
        ).fetchone()
    )
    waiting = any(
        json.loads(row["payload"]).get("name", "").casefold() == name.casefold()
        for row in db.execute(
            "SELECT payload FROM change_requests WHERE state='open' AND kind=?", (kind,)
        )
    )
    if taken or waiting:
        raise Refused(
            f"{'An agent' if kind == 'agent' else 'A team'} named “{name}” already exists or waits for the owner (archived ones "
            "count). Nothing was filed: choose another name."
        )


def file_agent(store, args, allowed, *, session_id, turn_id) -> dict:
    """File a request for a new agent; `allowed` are the tool ids the caller may hand on."""
    name = _text(args, "name", 80)
    configuration = agents.AgentInput(
        name=name,
        role=_text(args, "role", 160),
        instructions=_text(args, "instructions", 8000),
        modelId=None,
        toolIds=_tools(args.get("toolIds"), allowed),
        toolAccess={},
        memory=agents.MemorySelection(scope="conversation", memoryIds=[]),
    )
    with store._connect() as db:
        db.execute("BEGIN")
        _name_free(db, "agent", configuration.name)
    tools = ", ".join(configuration.toolIds) or "none"
    return change_requests.file(
        store,
        "agent",
        f"Create the agent “{configuration.name}”",
        f"{configuration.role}\nTools: {tools}\n\n{configuration.instructions}",
        configuration.model_dump(),
        session_id=session_id,
        turn_id=turn_id,
    )


def _agent(db, reference) -> dict:
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("Give each role's agent as an agent id or its exact name.")
    reference = reference.strip()
    row = db.execute(
        "SELECT id FROM agents WHERE id=? OR name_key=?", (reference, reference.casefold())
    ).fetchone()
    # An agent Conker kept from a forgotten or trashed chat carries that chat's words: unknown here.
    if row is None or row["id"] in visibility.kept_from_hidden(db, "agent"):
        raise Refused(f"There is no agent “{reference[:80]}”; nothing was filed.")
    agent = agents._get(db, row["id"])
    if agent["archived_at"] is not None:
        raise Refused(
            f"The agent “{agent['configuration']['name']}” is archived; nothing was filed."
        )
    return agent


def _definition(name, objective, roles) -> Team:
    count = len(roles)
    budget = {"maxTurns": 1, "maxTokens": ROLE_TOKENS, "maxCostCents": ROLE_CENTS}
    try:
        return Team.model_validate(
            {
                "name": name,
                "objective": objective,
                "roles": [
                    {
                        "id": f"role{index}",
                        "name": role_name,
                        "agentId": agent["id"],
                        "instructions": instructions,
                        "toolIds": agent["configuration"]["toolIds"],
                        "memory": {"scope": "none", "memoryIds": []},
                        "context": {"mode": "task_only", "sourceIds": []},
                        "budget": budget,
                    }
                    for index, (role_name, agent, instructions) in enumerate(roles, 1)
                ],
                "handoffs": [
                    {
                        "id": f"handoff{index}",
                        "fromRoleId": f"role{index}",
                        "toRoleId": f"role{index + 1}",
                        "condition": f"The owner reviewed the result of {roles[index - 1][0]}.",
                        "payload": "result_only",
                        "maxTransfers": 1,
                    }
                    for index in range(1, count)
                ],
                "budget": {
                    "maxTurns": count + 2,
                    "maxTokens": ROLE_TOKENS * count,
                    "maxCostCents": ROLE_CENTS * count,
                    "maxHandoffs": count - 1,
                },
            }
        )
    except ValidationError as invalid:
        first = invalid.errors()[0]
        raise ValueError(f"The team is not valid: {first['msg']}") from None


def file_team(store, args, *, session_id, turn_id) -> dict:
    """File a request for a new linear team of existing agents."""
    name = _text(args, "name", 80)
    objective = _text(args, "objective", 2000)
    given = args.get("roles")
    if not isinstance(given, list) or not MIN_ROLES <= len(given) <= MAX_ROLES:
        raise Refused(
            f"A team has {MIN_ROLES} to {MAX_ROLES} roles, each {{name, agent, instructions}}; "
            "nothing was filed."
        )
    with store._connect() as db:
        db.execute("BEGIN")
        roles = []
        for value in given:
            if not isinstance(value, dict):
                raise ValueError("Give each role as {name, agent, instructions}.")
            roles.append(
                (
                    _text(value, "name", 80),
                    _agent(db, value.get("agent")),
                    _text(value, "instructions", 8000),
                )
            )
        definition = _definition(name, objective, roles)
        _name_free(db, "team", definition.name)
        try:
            teams._team_agents(db, definition)
        except agents.AgentError as refusal:
            raise Refused(f"{refusal.message} Nothing was filed.") from None
    chain = " → ".join(role.name for role in definition.roles)
    lines = [
        f"{index}. {role_name} ({agent['configuration']['name']}): {instructions[:120]}"
        for index, (role_name, agent, instructions) in enumerate(roles, 1)
    ]
    return change_requests.file(
        store,
        "team",
        f"Create the team “{definition.name}”: {chain}",
        "\n".join([definition.objective[:300], *lines]),
        definition.model_dump(),
        session_id=session_id,
        turn_id=turn_id,
    )


def _checked(kind, payload):
    """The filed agent or team, refused if it carries more than these tools ever file."""
    if kind == "agent":
        body = agents.AgentInput.model_validate(payload)
        if (
            body.memory.scope != "conversation"
            or body.toolAccess
            or body.modelId is not None
            or NEVER_GIVEN & set(body.toolIds)
        ):
            raise ValueError("Not an agent Conker may ask for.")
        return body
    body = Team.model_validate(payload)
    if any(role.memory.scope != "none" for role in body.roles):
        raise ValueError("Not a team Conker may ask for.")
    return body


def approve(store, request, *, by, now=None) -> dict:
    """Make the filed agent or team, with the decision in the same transaction. A name taken or an
    agent archived since leaves the request open (409) for him to decline or fix."""
    now = time.time() if now is None else now
    identity, kind = request["id"], request["kind"]
    try:
        body = _checked(kind, request["payload"])
    except ValueError:
        return change_requests._close(
            store, identity, "failed", {"reason": f"The requested {kind} is no longer valid."}, now
        )
    field = "agentId" if kind == "agent" else "teamId"

    def decided(db, made):
        result = {field: made["id"]}
        if not db.execute(
            "UPDATE change_requests SET state='approved', result=?, decided_at=? "
            "WHERE id=? AND state='open'",
            (json.dumps(result), now, identity),
        ).rowcount:
            raise change_requests.ChangeRequestError(
                "decided", "This request was already decided.", 409
            )
        change_requests._keep(db, request, by, result, now)

    try:
        if kind == "agent":
            agents.create(store, body, then=decided)
        else:
            teams.save(store, "team", body, then=decided)
    except agents.AgentError as refusal:
        raise change_requests.ChangeRequestError(refusal.code, refusal.message, 409) from None
    return change_requests.get(store, identity)


def made(request) -> str | None:
    """What Conker tells the owner once it made one at once; None for other kinds."""
    name = request["payload"].get("name", "")
    if request["kind"] == "agent":
        return (
            f"Made the agent “{name}”. It is in Agents and listed in the owner's Inbox under "
            "Handled, where they can undo it (archive it)."
        )
    if request["kind"] == "team":
        return (
            f"Made the team “{name}”. It is in Teams and listed in the owner's Inbox under "
            "Handled, where they can undo it (archive it). Running it is the owner's: each "
            "handoff waits for their review."
        )
    return None


def archive_in(db, kind, identity) -> int:
    """Archive an agent or team inside the caller's transaction (forgetting the chat that made
    it); 1 if it was archived now, 0 if already archived or gone."""
    if kind == "agent":
        try:
            current = agents._get(db, identity)
        except agents.AgentError:
            return 0
        if current["archived_at"] is not None:
            return 0
        agents._append(
            db,
            current,
            agents.AgentInput.model_validate(current["configuration"]),
            time.time(),
            "archived",
        )
        return 1
    now = time.time()
    return db.execute(
        "UPDATE collaboration_records SET archived_at=?,revision=revision+1,updated_at=? "
        "WHERE id=? AND kind='team' AND archived_at IS NULL AND deleted_at IS NULL",
        (now, now, identity),
    ).rowcount
