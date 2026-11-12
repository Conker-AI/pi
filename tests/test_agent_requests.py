"""Conker asks for an agent or a team; it exists only once he approves (or his settings let it).

The guards: a new agent gets only tools its caller can use, never the create or delegation tools,
no per-tool access and only its own chat's memory; a team is 2 to 6 roles of existing, active
agents; names are unique; his typed words never approve one on their own.
"""

import json
import sqlite3

import pytest
from test_tool_turns import FakeGate, build

from pi.agents import agent_requests, change_requests, keep, teams
from pi.agents import profiles as agents
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.turns import access, native_tools

AGENT = {
    "name": "Researcher",
    "role": "Finds sources",
    "instructions": "Search, then list the three best sources with one line each.",
}


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "pi.db")
    yield value
    value.close()


def call(loop, tool_id, args, session, *, run=False, execution=None):
    execution = execution or session_settings.execution(loop.store, session)
    return native_tools.call(
        loop, tool_id, args, session_id=session, turn_id=None, execution=execution, run=run
    )


def chat(store, *, agent_id="companion", level="default", memory_off=False):
    session = store.create_session()
    session_settings.save(
        store,
        session,
        session_settings.Update(
            expected_revision=0,
            settings=session_settings.Settings(
                agentId=agent_id,
                privacy=session_settings.Privacy(memoryDisabled=memory_off, harnessDisabled=False),
                access=level,
            ),
        ),
    )
    return session


def owner_agent(store, name="Writer", tools=("t_echo",)):
    return agents.create(
        store,
        agents.AgentInput(
            name=name,
            role="Writes",
            instructions="Write clearly.",
            modelId=None,
            toolIds=list(tools),
            memory=agents.MemorySelection(scope="conversation", memoryIds=[]),
        ),
    )


def named(store, name):
    return [a for a in agents.list_agents(store)["results"] if a["configuration"]["name"] == name]


def team_args(store, count=2):
    for index in range(count):
        if not named(store, f"Helper {index}"):
            owner_agent(store, f"Helper {index}")
    return {
        "name": "Review crew",
        "objective": "Draft and check a summary.",
        "roles": [
            {"name": f"Step {index}", "agent": f"Helper {index}", "instructions": f"Do {index}."}
            for index in range(count)
        ],
    }


# --- filing and approving --------------------------------------------------------------------


def test_an_agent_is_only_filed_until_he_approves_it(store):
    loop, _ = build(store, [], FakeGate())
    result = call(loop, native_tools.CREATE_AGENT, {**AGENT, "toolIds": ["t_echo"]}, chat(store))
    assert result.ok and result.result["status"] == "waiting_for_owner"
    assert named(store, "Researcher") == []
    [request] = change_requests.listing(store)
    assert request["kind"] == "agent" and request["title"] == "Create the agent “Researcher”"
    approved = change_requests.approve(store, request["id"])
    [agent] = named(store, "Researcher")
    assert approved["state"] == "approved" and approved["result"] == {"agentId": agent["id"]}
    configuration = agent["configuration"]
    assert configuration["toolIds"] == ["t_echo"] and configuration["toolAccess"] == {}
    assert configuration["memory"] == {"scope": "conversation", "memoryIds": []}
    assert configuration["modelId"] is None
    assert keep.listing(store, request["sessionId"]) == []  # his approval is no Keep result


def test_a_full_access_chat_makes_it_at_once_listed_in_handled_and_undo_archives(store):
    call_text = json.dumps({"tool": native_tools.CREATE_AGENT, "args": AGENT})
    loop, _ = build(store, [call_text, "Made it."], FakeGate())
    session = chat(store, level="full")
    loop.run_turn(session, "Make me a research helper.")
    [agent] = named(store, "Researcher")
    [made] = change_requests.listing(store, state="approved")
    assert made["result"] == {"agentId": agent["id"]}
    [handled] = access.handled(store, 0)
    assert handled["toolId"] == native_tools.CREATE_AGENT and handled["decidedBy"] == "chat"
    [row] = keep.listing(store, session)
    assert (row["kind"], row["target_id"]) == ("agent", agent["id"])
    # Undo is archiving it; forgetting the chat archives it too.
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        counts = keep.forget_chats(db, [session])
        db.commit()
    assert counts["agents"] == 1
    assert agents.get(store, agent["id"])["archived_at"] is not None


def test_the_tool_result_says_it_was_made(store):
    loop, _ = build(store, [], FakeGate())
    done = call(loop, native_tools.CREATE_AGENT, AGENT, chat(store), run=True)
    assert done.result["status"] == "done"
    assert done.result["message"].startswith("Made the agent “Researcher”.")


def test_his_typed_words_do_not_approve_it_and_no_reviewer_is_asked(store):
    call_text = json.dumps({"tool": native_tools.CREATE_AGENT, "args": AGENT})
    loop, provider = build(store, [call_text, "It waits in your Inbox."], FakeGate())
    loop.run_turn(chat(store), "Create an agent called Researcher that finds sources.")
    assert named(store, "Researcher") == [] and access.handled(store, 0) == []
    [request] = change_requests.listing(store)
    assert request["kind"] == "agent" and len(provider.sent) == 2  # no reviewer call


def test_decide_asks_for_both_tools_whatever_he_typed(store):
    session = store.create_session()
    turn = store.start_turn(session)
    store.append_message(session, "user", "Create the agent now.", turn_id=turn, purpose="input")
    loop, provider = build(store, [], FakeGate())
    for tool in native_tools.CATALOG:
        if tool.id not in (native_tools.CREATE_AGENT, native_tools.CREATE_TEAM):
            continue
        assert tool.authorization == "owner_confirmation"
        choice, _, _ = access.decide(
            loop,
            tool,
            AGENT,
            execution={"settings": {"access": "default"}, "configuration": {}},
            session_id=session,
            turn_id=turn,
            native=native_tools.owns,
            delegate=lambda: False,
        )
        assert choice == "ask"
    assert provider.sent == []


# --- tool guards ----------------------------------------------------------------------------


def test_a_non_companion_caller_cannot_hand_on_tools_outside_its_toolkit(store):
    helper = owner_agent(store, "Helper", tools=[native_tools.CREATE_AGENT])
    session = chat(store, agent_id=helper["id"])
    loop, _ = build(store, [], FakeGate())
    refused = call(loop, native_tools.CREATE_AGENT, {**AGENT, "toolIds": ["t_echo"]}, session)
    assert refused.result["status"] == "refused" and "t_echo" in refused.result["message"]
    assert change_requests.listing(store) == []


def test_the_companion_may_hand_on_only_tools_that_exist(store):
    loop, _ = build(store, [], FakeGate())
    session = chat(store)
    unknown = call(loop, native_tools.CREATE_AGENT, {**AGENT, "toolIds": ["mail.send"]}, session)
    assert unknown.result["status"] == "refused"
    known = {**AGENT, "toolIds": ["t_echo", native_tools.SCHEDULE]}
    assert call(loop, native_tools.CREATE_AGENT, known, session).result["status"] == (
        "waiting_for_owner"
    )


def test_create_and_delegation_tools_are_always_left_out(store):
    loop, _ = build(store, [], FakeGate())
    tools = ["t_echo", *sorted(agent_requests.NEVER_GIVEN)]
    call(loop, native_tools.CREATE_AGENT, {**AGENT, "toolIds": tools}, chat(store), run=True)
    [agent] = named(store, "Researcher")
    assert agent["configuration"]["toolIds"] == ["t_echo"]


def test_a_request_carrying_more_fails_at_approval(store):
    payload = {
        **AGENT,
        "modelId": None,
        "toolIds": [],
        "toolAccess": {},
        "memory": {"scope": "owner", "memoryIds": []},
    }
    request = change_requests.file(store, "agent", "Create the agent", "", payload)
    assert change_requests.approve(store, request["id"])["state"] == "failed"
    granted = {**payload, "memory": {"scope": "conversation", "memoryIds": []}}
    for widened in (
        {**granted, "toolAccess": {"t_echo": "allow"}},
        {**granted, "toolIds": [native_tools.CREATE_TEAM]},
    ):
        request = change_requests.file(store, "agent", "Create the agent", "", widened)
        assert change_requests.approve(store, request["id"])["state"] == "failed"
    assert named(store, "Researcher") == []


def test_a_duplicate_name_is_refused_when_filed(store):
    loop, _ = build(store, [], FakeGate())
    owner_agent(store, "researcher")
    refused = call(loop, native_tools.CREATE_AGENT, AGENT, chat(store))
    assert refused.result["status"] == "refused" and "already exists" in refused.result["message"]
    other = {**AGENT, "name": "Scout"}
    assert call(loop, native_tools.CREATE_AGENT, other, chat(store)).result["status"] == (
        "waiting_for_owner"
    )
    again = call(loop, native_tools.CREATE_AGENT, other, chat(store))
    assert again.result["status"] == "refused"  # one already waits for him


def test_a_name_taken_after_filing_leaves_the_request_open(store):
    loop, _ = build(store, [], FakeGate())
    call(loop, native_tools.CREATE_AGENT, AGENT, chat(store))
    [request] = change_requests.listing(store)
    owner_agent(store, "Researcher")
    with pytest.raises(change_requests.ChangeRequestError) as taken:
        change_requests.approve(store, request["id"])
    assert taken.value.code == "name_conflict"
    assert change_requests.get(store, request["id"])["state"] == "open"


def test_private_chats_and_job_runs_never_offer_them(store):
    loop, _ = build(store, [], FakeGate())
    private = chat(store, memory_off=True)
    offered = {t.id for t in native_tools.offered(loop, None, private, None)}
    assert not offered & {native_tools.CREATE_AGENT, native_tools.CREATE_TEAM}
    refused = call(loop, native_tools.CREATE_TEAM, team_args(store), private, run=True)
    assert refused.result["status"] == "refused" and "private" in refused.result["message"]
    assert {native_tools.CREATE_AGENT, native_tools.CREATE_TEAM} <= native_tools.JOB_REFUSED


def test_the_per_chat_request_limit_still_applies(store):
    loop, _ = build(store, [], FakeGate())
    session = chat(store)
    for index in range(change_requests.PER_CHAT):
        args = {**AGENT, "name": f"Agent {index}"}
        assert call(loop, native_tools.CREATE_AGENT, args, session).result["status"] == (
            "waiting_for_owner"
        )
    full = call(loop, native_tools.CREATE_AGENT, {**AGENT, "name": "One more"}, session)
    assert full.result["status"] == "refused" and "already wait" in full.result["message"]


# --- teams ----------------------------------------------------------------------------------


def test_a_team_is_linear_with_its_agents_tools_and_a_small_budget(store):
    loop, _ = build(store, [], FakeGate())
    args = team_args(store, 3)
    filed = call(loop, native_tools.CREATE_TEAM, args, chat(store))
    assert filed.result["status"] == "waiting_for_owner"
    [request] = change_requests.listing(store)
    assert request["title"] == "Create the team “Review crew”: Step 0 → Step 1 → Step 2"
    made = change_requests.approve(store, request["id"])
    definition = teams.owner_get(store, made["result"]["teamId"]).definition
    assert [(h.fromRoleId, h.toRoleId) for h in definition.handoffs] == [
        ("role1", "role2"),
        ("role2", "role3"),
    ]
    assert {h.payload for h in definition.handoffs} == {"result_only"}
    assert definition.budget.maxTurns == 5 and definition.budget.maxHandoffs == 2
    assert definition.budget.maxCostCents > 0  # zero would stop the first model call
    assert all(role.toolIds == ["t_echo"] for role in definition.roles)
    assert all(role.memory.scope == "none" for role in definition.roles)


def test_a_team_made_at_once_is_archived_when_its_chat_is_forgotten(store):
    loop, _ = build(store, [], FakeGate())
    session = chat(store)
    done = call(loop, native_tools.CREATE_TEAM, team_args(store), session, run=True)
    assert done.result["status"] == "done" and "Running it is the owner's" in done.result["message"]
    team_id = done.result["made"]["teamId"]
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        assert keep.forget_chats(db, [session])["teams"] == 1
        db.commit()
    assert teams.get(store, team_id, "team")["archived_at"] is not None


def test_a_team_with_an_unknown_or_archived_agent_is_refused(store):
    loop, _ = build(store, [], FakeGate())
    args = team_args(store)
    args["roles"][1]["agent"] = "Nobody"
    refused = call(loop, native_tools.CREATE_TEAM, args, chat(store))
    assert refused.result["status"] == "refused" and "Nobody" in refused.result["message"]
    gone = owner_agent(store, "Retired")
    agents.archive(store, gone["id"], agents.ArchiveAgent(expected_revision=1, archived=True))
    args["roles"][1]["agent"] = gone["id"]
    archived = call(loop, native_tools.CREATE_TEAM, args, chat(store))
    assert archived.result["status"] == "refused" and "archived" in archived.result["message"]
    assert change_requests.listing(store) == []


@pytest.mark.parametrize("count", [1, 7])
def test_a_team_needs_two_to_six_roles(store, count):
    loop, _ = build(store, [], FakeGate())
    refused = call(loop, native_tools.CREATE_TEAM, team_args(store, count), chat(store))
    assert refused.result["status"] == "refused" and "2 to 6" in refused.result["message"]
    assert change_requests.listing(store) == []


# --- migration ------------------------------------------------------------------------------

OLD_REQUESTS = """
CREATE TABLE change_requests (
 id TEXT PRIMARY KEY,
 kind TEXT NOT NULL CHECK(kind IN ('ai_job','tool_draft','memory')),
 title TEXT NOT NULL, summary TEXT NOT NULL, payload TEXT NOT NULL,
 session_id TEXT REFERENCES sessions(id), turn_id TEXT,
 state TEXT NOT NULL CHECK(state IN ('open','approved','declined','failed')),
 result TEXT, created_at REAL NOT NULL, decided_at REAL
);
CREATE INDEX change_requests_open ON change_requests(state, created_at);
CREATE TABLE keep_results (
 request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
 kind TEXT NOT NULL CHECK(kind IN ('memory','ai_job')), target_id TEXT NOT NULL,
 created_at REAL NOT NULL,
 undo TEXT CHECK(undo IN ('pending','done','gone','blocked')),
 revision INTEGER, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT ''
);
"""


def test_an_older_database_keeps_its_rows_and_takes_the_new_kinds(tmp_path):
    path = tmp_path / "pi.db"
    Store(path).close()
    db = sqlite3.connect(path)
    db.executescript(
        "DROP TABLE change_requests; DROP TABLE keep_results;"
        + OLD_REQUESTS
        + "INSERT INTO sessions(id,created_at) VALUES ('s1',1);"
        "INSERT INTO change_requests VALUES ('creq_" + "a" * 32 + "','memory','Remember',"
        "'x','{\"text\": \"x\"}','s1',NULL,'open',NULL,1,NULL);"
        "INSERT INTO keep_results(request_id,session_id,kind,target_id,created_at) "
        "VALUES ('creq_" + "b" * 32 + "','s1','ai_job','job_1',1);"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO change_requests VALUES ('c2','agent','t','s','{}',NULL,NULL,'open',"
            "NULL,1,NULL)"
        )
    db.close()
    for _ in range(2):  # the second start finds it done and leaves it
        store = Store(path)
        [old] = change_requests.listing(store)
        assert (old["kind"], old["sessionId"], old["payload"]) == ("memory", "s1", {"text": "x"})
        assert [row["target_id"] for row in keep.listing(store, "s1")] == ["job_1"]
        with store._connect() as conn:
            indexes = {
                r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
            }
            assert {"change_requests_open", "keep_results_session"} <= indexes
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        store.close()
    store = Store(path)
    loop, _ = build(store, [], FakeGate())
    done = call(loop, native_tools.CREATE_AGENT, AGENT, chat(store), run=True)
    assert done.result["status"] == "done"
    store.close()
