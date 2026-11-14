"""Follow-ups to the kept-definitions lists (pi.conversation.visibility.kept_from_hidden):

- a team preparation freezes the team's definition and each role's agent configuration, so a
  preparation of a team, or of a role agent, Conker kept from a hidden chat leaves GET
  /collaboration with the team itself;
- an AI job the owner adopted (agent_jobs.adopt) is his: it stays in his lists and briefing when
  the chat it was kept from is trashed or forgotten, as forgetting leaves it on (turn_off)."""

from contextlib import closing

import pytest

from pi.agents import continuity, definitions, keep, teams
from pi.agents import profiles as agents
from pi.agents.team_models import Revision, Team
from pi.context import projects
from pi.conversation import archive, forgetting
from pi.conversation.store import Store
from pi.schedule import agent_jobs, jobs
from pi.schedule.job_models import Adopt, AgentJobCreate, Timing


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def kept_in(session, kind):
    def record(db, result):
        request = {"id": f"req_{kind}_{result['id']}", "sessionId": session, "kind": kind}
        keep.record(db, request, {keep.TARGETS[kind]: result["id"]})

    return record


def make_agent(store, name, session=None):
    body = agents.AgentInput(
        name=name,
        role=f"{name} role",
        instructions=f"{name} instructions.",
        modelId=None,
        toolIds=["read"],
        memory=agents.MemorySelection(scope="conversation", memoryIds=[]),
    )
    return agents.create(store, body, then=kept_in(session, "agent") if session else None)["id"]


def make_team(store, name, agent_id, session=None):
    budget = {"maxTurns": 2, "maxTokens": 100, "maxCostCents": 0}
    definition = Team.model_validate(
        {
            "name": name,
            "objective": f"{name} objective",
            "roles": [
                {
                    "id": "reader",
                    "name": "Reader",
                    "agentId": agent_id,
                    "instructions": "Report.",
                    "toolIds": ["read"],
                    "memory": {"scope": "conversation", "memoryIds": []},
                    "context": {"mode": "task_only", "sourceIds": []},
                    "budget": budget,
                }
            ],
            "handoffs": [],
            "budget": {**budget, "maxHandoffs": 0},
        }
    )
    then = kept_in(session, "team") if session else None
    return teams.save(store, "team", definition, then=then)


def prepare(store, made):
    return teams.prepare(store, made["id"], "team", Revision(expected_revision=made["revision"]))


def prepared(store):
    return {p["id"] for p in teams.list_all(store)["preparations"]}


def trash(store, session, trashed=True):
    archive.trash(store, archive.Trash(sessions=[session], trashed=trashed))


def test_a_kept_teams_preparation_leaves_the_collaboration_list_with_the_team(store):
    hidden_chat = store.create_session(title="H")
    agent = make_agent(store, "Plain agent")
    kept_team = make_team(store, "Secret team", agent, hidden_chat)
    preparation = prepare(store, kept_team)["id"]
    assert preparation in prepared(store)

    trash(store, hidden_chat)
    listing = teams.list_all(store)
    assert preparation not in {p["id"] for p in listing["preparations"]}
    assert "Secret" not in str(listing)

    trash(store, hidden_chat, trashed=False)
    assert preparation in prepared(store)


def test_a_visible_teams_preparation_that_froze_a_kept_agent_leaves_the_list(store):
    hidden_chat = store.create_session(title="H")
    kept_agent = make_agent(store, "Secret agent", hidden_chat)
    plain_agent = make_agent(store, "Plain agent")
    owners_team = make_team(store, "Owner team", kept_agent)
    other_team = make_team(store, "Other team", plain_agent)
    with_kept, without = prepare(store, owners_team)["id"], prepare(store, other_team)["id"]

    trash(store, hidden_chat)
    listing = teams.list_all(store)
    assert {p["id"] for p in listing["preparations"]} == {without}
    assert owners_team["id"] in {t["id"] for t in listing["teams"]}
    assert "Secret" not in str(listing)

    trash(store, hidden_chat, trashed=False)
    assert prepared(store) == {with_kept, without}


def adopted_job(store, session):
    timing = Timing(kind="daily", time="09:00", day=0, hours=24)
    body = AgentJobCreate(
        name="Kept job", instructions="Kept instructions.", timing=timing, timeZone="UTC"
    )
    made = agent_jobs.create(store, body, created_by="conker", then=kept_in(session, "ai_job"))
    run = jobs.run_now(store, made["id"], f"adopted-run-{made['id']}")
    with store._connect() as db:
        db.execute("UPDATE scheduled_runs SET status='failed' WHERE id=?", (run["id"],))
    revision = jobs.get_job(store, made["id"])["revision"]
    agent_jobs.adopt(store, made["id"], Adopt(expected_revision=revision))
    assert jobs.get_job(store, made["id"])["definition"]["createdBy"] == "owner"
    return made["id"]


def shown(store, job_id):
    summaries = definitions.summaries(store, archived=True, list_projects=projects.list_projects)[
        "results"
    ]
    briefing = continuity.briefing(store, limit=100)["items"]
    return {
        "jobs": job_id in {j["id"] for j in jobs.list_jobs(store)},
        "definitions": ("job", job_id) in {(r["kind"], r["id"]) for r in summaries},
        "briefing": job_id in {i.get("jobId") for i in briefing if i["kind"] == "job_status"},
    }


EVERYWHERE = {"jobs": True, "definitions": True, "briefing": True}


def test_a_job_the_owner_adopted_stays_his_while_its_chat_is_in_the_trash(store):
    hidden_chat = store.create_session(title="H")
    job_id = adopted_job(store, hidden_chat)
    assert shown(store, job_id) == EVERYWHERE

    trash(store, hidden_chat)
    assert shown(store, job_id) == EVERYWHERE
    # A job he did not adopt from the same chat still leaves the lists.
    timing = Timing(kind="daily", time="10:00", day=0, hours=24)
    body = AgentJobCreate(name="Other", instructions="Other.", timing=timing, timeZone="UTC")
    other = agent_jobs.create(store, body, created_by="conker", then=kept_in(hidden_chat, "ai_job"))
    assert other["id"] not in {j["id"] for j in jobs.list_jobs(store)}


def test_a_job_the_owner_adopted_stays_listed_and_on_after_its_chat_is_forgotten(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        chat = store.create_session(title="H")
        job_id = adopted_job(store, chat)
    plan = forgetting.preview(path, chat)
    forgetting.forget(path, chat, plan["confirmation"])
    with closing(Store(path)) as store:
        assert jobs.get_job(store, job_id)["definition"]["enabled"] is True
        assert shown(store, job_id) == EVERYWHERE
