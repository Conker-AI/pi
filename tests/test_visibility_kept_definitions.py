"""An agent, team or AI job Conker kept from a chat leaves the owner's lists, the definitions list
and the briefing while that chat is forgotten or in the Trash, and comes back when it is restored
(pi.conversation.visibility.kept_from_hidden). Opened by its own id it still shows."""

from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pi.agents import continuity, definitions, keep, teams
from pi.agents import profiles as agents
from pi.agents.team_models import Team
from pi.api import server as api
from pi.context import projects
from pi.conversation import archive, forgetting
from pi.conversation.store import Store
from pi.schedule import agent_jobs, jobs
from pi.schedule.job_models import AgentJobCreate, Timing

ADMIN = "synthetic-admin-key-for-kept-lists"


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def kept_in(session, kind):
    """`then` for a create: record the result as Conker's Keep from `session`."""

    def record(db, result):
        request = {"id": f"req_{kind}_{session}", "sessionId": session, "kind": kind}
        keep.record(db, request, {keep.TARGETS[kind]: result["id"]})

    return record


def agent_input(name):
    return agents.AgentInput(
        name=name,
        role=f"{name} role",
        instructions=f"{name} instructions.",
        modelId=None,
        toolIds=["read"],
        memory=agents.MemorySelection(scope="conversation", memoryIds=[]),
    )


def team(name, agent_id):
    budget = {"maxTurns": 2, "maxTokens": 100, "maxCostCents": 0}
    return Team.model_validate(
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


def job(name):
    timing = Timing(kind="daily", time="09:00", day=0, hours=24)
    return AgentJobCreate(
        name=name, instructions=f"{name} instructions.", timing=timing, timeZone="UTC"
    )


def failed_run(store, job_id):
    """A run of the job that ended failed, so the briefing has a job_status item for it."""
    run = jobs.run_now(store, job_id, f"kept-lists-run-{job_id}")
    with store._connect() as db:
        db.execute("UPDATE scheduled_runs SET status='failed' WHERE id=?", (run["id"],))


def make(store, session, label):
    """An agent, a team and an AI job Conker kept from `session`, each named `label`."""
    agent = agents.create(store, agent_input(f"{label} agent"), then=kept_in(session, "agent"))
    made = teams.save(
        store, "team", team(f"{label} team", agent["id"]), then=kept_in(session, "team")
    )
    ai_job = agent_jobs.create(
        store, job(f"{label} job"), created_by="conker", then=kept_in(session, "ai_job")
    )
    failed_run(store, ai_job["id"])
    return {"agent": agent["id"], "team": made["id"], "job": ai_job["id"]}


def listed(store):
    """What each list shows, called directly."""
    summaries = definitions.summaries(store, archived=True, list_projects=projects.list_projects)[
        "results"
    ]
    return {
        "agents": {a["id"] for a in agents.list_agents(store)["results"]},
        "teams": {t.id for t in teams.owner_list(store).results},
        "all_teams": {t["id"] for t in teams.list_all(store)["teams"]},
        "jobs": {j["id"] for j in jobs.list_jobs(store)},
        "definitions": {(row["kind"], row["id"]) for row in summaries},
        "briefing": {
            item["jobId"]
            for item in continuity.briefing(store, limit=100)["items"]
            if item["kind"] == "job_status"
        },
    }


def shows(lists, made):
    """For each list, whether the made things are in it."""
    return {
        "agents": made["agent"] in lists["agents"],
        "teams": made["team"] in lists["teams"],
        "all_teams": made["team"] in lists["all_teams"],
        "jobs": made["job"] in lists["jobs"],
        "definitions": {("agent", made["agent"]), ("team", made["team"]), ("job", made["job"])}
        <= lists["definitions"],
        "definitions_none": not (
            {("agent", made["agent"]), ("team", made["team"]), ("job", made["job"])}
            & lists["definitions"]
        ),
        "briefing": made["job"] in lists["briefing"],
    }


EVERYWHERE = {
    "agents": True,
    "teams": True,
    "all_teams": True,
    "jobs": True,
    "definitions": True,
    "definitions_none": False,
    "briefing": True,
}
NOWHERE = {key: not value for key, value in EVERYWHERE.items()}


def trash(store, session, trashed=True):
    archive.trash(store, archive.Trash(sessions=[session], trashed=trashed))


def test_kept_definitions_leave_every_list_while_their_chat_is_in_the_trash(store):
    hidden_chat, other_chat = store.create_session(title="H"), store.create_session(title="V")
    hidden = make(store, hidden_chat, "Hidden")
    visible = make(store, other_chat, "Visible")
    assert shows(listed(store), hidden) == EVERYWHERE

    trash(store, hidden_chat)
    lists = listed(store)
    assert shows(lists, hidden) == NOWHERE
    assert shows(lists, visible) == EVERYWHERE
    assert "companion" in lists["agents"]
    # Opened by its own id each one still reads.
    assert agents.get(store, hidden["agent"])["id"] == hidden["agent"]
    assert teams.owner_get(store, hidden["team"]).id == hidden["team"]
    assert jobs.get_job(store, hidden["job"])["id"] == hidden["job"]

    trash(store, hidden_chat, trashed=False)
    assert shows(listed(store), hidden) == EVERYWHERE


def test_team_pages_skip_a_hidden_team_without_losing_a_visible_one(store):
    hidden_chat, other_chat = store.create_session(title="H"), store.create_session(title="V")
    made = [make(store, chat, label) for chat, label in ((hidden_chat, "A"), (other_chat, "B"))]
    trash(store, hidden_chat)
    first = teams.owner_list(store, limit=1)
    assert [t.id for t in first.results] == [made[1]["team"]] and first.nextCursor is None


def test_the_owner_routes_leave_out_what_a_trashed_chat_kept(store, monkeypatch):
    monkeypatch.setattr(api.app.state, "store", store, raising=False)
    monkeypatch.setattr(api.app.state, "admin_key", ADMIN, raising=False)
    client = TestClient(api.app)
    headers = {"X-Pi-Key": ADMIN}
    hidden_chat = store.create_session(title="H")
    hidden = make(store, hidden_chat, "Hidden")

    def ids():
        def get(path):
            response = client.get(path, headers=headers)
            assert response.status_code == 200, (path, response.text)
            return response.json()

        return {
            "agents": {a["id"] for a in get("/agents")["results"]},
            "teams": {t["id"] for t in get("/collaboration/teams")["results"]},
            "all_teams": {t["id"] for t in get("/collaboration")["teams"]},
            "jobs": {j["id"] for j in get("/jobs")["results"]},
            "definitions": {r["id"] for r in get("/definitions?archived=true")["results"]},
            "briefing": {
                i.get("jobId") for i in get("/continuity?limit=100")["items"] if i.get("jobId")
            },
        }

    def present():
        found = ids()
        return {
            "agents": hidden["agent"] in found["agents"],
            "teams": hidden["team"] in found["teams"],
            "all_teams": hidden["team"] in found["all_teams"],
            "jobs": hidden["job"] in found["jobs"],
            "definitions": {hidden["agent"], hidden["team"], hidden["job"]} & found["definitions"],
            "briefing": hidden["job"] in found["briefing"],
        }

    everywhere = {
        "agents": True,
        "teams": True,
        "all_teams": True,
        "jobs": True,
        "definitions": {hidden["agent"], hidden["team"], hidden["job"]},
        "briefing": True,
    }
    assert present() == everywhere
    trash(store, hidden_chat)
    assert present() == {
        "agents": False,
        "teams": False,
        "all_teams": False,
        "jobs": False,
        "definitions": set(),
        "briefing": False,
    }
    assert client.get(f"/jobs/{hidden['job']}", headers=headers).status_code == 200
    trash(store, hidden_chat, trashed=False)
    assert present() == everywhere


def test_a_forgotten_chat_keeps_its_definitions_out_of_the_lists(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        chat = store.create_session(title="H")
        made = make(store, chat, "Gone")
    plan = forgetting.preview(path, chat)
    forgetting.forget(path, chat, plan["confirmation"])
    with closing(Store(path)) as store:
        assert shows(listed(store), made) == NOWHERE
        # Forgetting turned the job off and archived the agent; neither was deleted.
        assert jobs.get_job(store, made["job"])["definition"]["enabled"] is False
        assert agents.get(store, made["agent"])["archived_at"] is not None
