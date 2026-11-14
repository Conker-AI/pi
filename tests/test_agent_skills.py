"""An agent carries up to three skills by reference; they load like the picker's, grant no tool."""

from contextlib import closing

import pytest
from test_loop import Recorder, loop_with
from test_skills import document, imported

from pi.actions.toolgate import Tool
from pi.agents import profiles as agents
from pi.agents import skills
from pi.conversation import settings as session_settings
from pi.conversation import submissions
from pi.conversation.store import Store


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "agent-skills.db")) as value:
        yield value


def configuration(skill_ids=(), name="Reviewer"):
    return agents.AgentInput(
        name=name,
        role="Review work",
        instructions="Review what the owner gives you.",
        modelId=None,
        toolIds=[],
        memory=agents.MemorySelection(scope="conversation", memoryIds=[]),
        skillIds=list(skill_ids),
    )


def chat_with(store, agent, harness_disabled=False):
    sid = store.create_session()
    session_settings.save(
        store,
        sid,
        session_settings.Update(
            expected_revision=0,
            settings=session_settings.Settings(
                agentId=agent["id"],
                privacy=session_settings.Privacy(
                    memoryDisabled=False, harnessDisabled=harness_disabled
                ),
            ),
        ),
    )
    return sid


def test_agent_skill_reaches_the_model_and_is_frozen_with_the_turn(store):
    original = imported(store, body="Use the agent checklist.")
    agent = agents.create(store, configuration(["review"]))
    assert agent["configuration"]["skillIds"] == ["review"]
    provider = Recorder()
    loop = loop_with(store, provider)
    sid = chat_with(store, agent)
    result = loop.run_turn(sid, "Look at this", request_id="agent_skill_turn")
    execution = session_settings.execution(store, sid, turn_id=result["turn_id"])
    assert execution["skills"] == [{**original, "source": "agent"}]
    supplied = [message.content for message in provider.calls[0]]
    assert any("Use the agent checklist." in text for text in supplied)
    skills.import_skill(
        store, skills.Import(markdown=document(body="Changed later."), expected_revision=1)
    )
    again = session_settings.execution(store, sid, turn_id=result["turn_id"])
    assert again["skills"][0]["markdown"] == original["markdown"]


def test_a_skill_grants_no_tool(store):
    imported(store)
    agent = agents.create(store, configuration(["review"]))
    loop = loop_with(store, Recorder())

    class Gate:
        def tools(self):
            return [Tool("read", "Read", "Existing permitted read", [])]

    loop.toolgate = Gate()
    before = [tool.id for tool in loop._available_tools()]
    sid = chat_with(store, agent)
    result = loop.run_turn(sid, "Look at this", request_id="agent_skill_tools")
    execution = session_settings.execution(store, sid, turn_id=result["turn_id"])
    assert execution["configuration"]["toolIds"] == []
    assert execution["authority"] == "none"
    assert [tool.id for tool in loop._available_tools()] == before


def test_message_picks_join_the_agents_skills_once(store):
    imported(store, "review", "Agent checklist.")
    imported(store, "tone", "Be brief.")
    agent = agents.create(store, configuration(["review"]))
    sid = chat_with(store, agent)
    submissions.reserve(store, "pick_and_carried", sid, "Look", {}, skill_ids=["review", "tone"])
    bound = submissions.bind(store, "pick_and_carried")
    execution = session_settings.execution(store, sid, turn_id=bound["turn_id"])
    assert [(item["id"], item.get("source")) for item in execution["skills"]] == [
        ("review", "agent"),
        ("tone", None),
    ]
    # A retry offers back only what the owner picked, never the agent's own.
    assert submissions.get(store, "pick_and_carried")["skill_ids"] == ["tone"]


def test_agent_skills_load_without_a_pick(store):
    imported(store)
    agent = agents.create(store, configuration(["review"]))
    sid = chat_with(store, agent)
    submissions.reserve(store, "carried_only_request", sid, "Look", {})
    bound = submissions.bind(store, "carried_only_request")
    execution = session_settings.execution(store, sid, turn_id=bound["turn_id"])
    assert [item["id"] for item in execution["skills"]] == ["review"]
    assert submissions.get(store, "carried_only_request")["skill_ids"] == []


def test_no_harness_chat_leaves_agent_skills_out(store):
    imported(store)
    agent = agents.create(store, configuration(["review"]))
    sid = chat_with(store, agent, harness_disabled=True)
    submissions.reserve(store, "private_chat_request", sid, "Look", {})
    bound = submissions.bind(store, "private_chat_request")
    assert "skills" not in session_settings.execution(store, sid, turn_id=bound["turn_id"])


def test_a_removed_or_disabled_skill_is_left_out_not_fatal(store):
    imported(store)
    imported(store, "tone", "Be brief.")
    agent = agents.create(store, configuration(["review", "tone"]))
    skills.set_state(store, "review", skills.State(expected_revision=1, enabled=False))
    sid = chat_with(store, agent)
    submissions.reserve(store, "one_off_skill_request", sid, "Look", {})
    bound = submissions.bind(store, "one_off_skill_request")
    execution = session_settings.execution(store, sid, turn_id=bound["turn_id"])
    assert [item["id"] for item in execution["skills"]] == ["tone"]
    assert execution["skillsSkipped"] == [{"id": "review", "reason": "turned off"}]


def test_attached_skills_over_the_combined_limit_are_refused_when_saved(store):
    for number in range(3):
        imported(store, f"skill-{number}", "x" * 22000)
    with pytest.raises(agents.AgentError) as error:
        agents.create(store, configuration(["skill-0", "skill-1", "skill-2"]))
    assert error.value.detail["code"] == "skills_limit"
    assert agents.create(store, configuration(["skill-0", "skill-1"]))["revision"] == 1


def test_a_skill_that_grew_since_is_skipped_and_recorded_not_fatal(store):
    for number in range(3):
        imported(store, f"skill-{number}", "x" * 20000)
    agent = agents.create(store, configuration(["skill-0", "skill-1", "skill-2"]))
    skills.import_skill(
        store,
        skills.Import(markdown=document("skill-2", "y" * 30000), expected_revision=1),
    )
    sid = chat_with(store, agent)
    # Reading the chat's settings (forks, the queue and so on) still works.
    assert session_settings.load(store, sid)["settings"]["agentId"] == agent["id"]
    submissions.reserve(store, "grown_skill_request", sid, "Look", {})
    bound = submissions.bind(store, "grown_skill_request")
    execution = session_settings.execution(store, sid, turn_id=bound["turn_id"])
    assert [item["id"] for item in execution["skills"]] == ["skill-0", "skill-1"]
    assert execution["skillsSkipped"] == [{"id": "skill-2", "reason": "over the combined limit"}]


def test_agent_skill_ids_are_checked_when_saved(store):
    imported(store)
    with pytest.raises(ValueError):
        configuration(["review"] * 2)
    with pytest.raises(ValueError):
        configuration(["review", "a", "b", "c"])
    with pytest.raises(ValueError):
        configuration(["Bad/Name"])
    with pytest.raises(agents.AgentError) as error:
        agents.create(store, configuration(["missing"]))
    assert error.value.detail["code"] == "unknown_skill"
    agent = agents.create(store, configuration(["review"], name="Other"))
    with pytest.raises(agents.AgentError) as error:
        agents.update(
            store,
            agent["id"],
            agents.UpdateAgent(
                expected_revision=agent["revision"], configuration=configuration(["missing"])
            ),
        )
    assert error.value.detail["code"] == "unknown_skill"


def test_an_agent_without_skills_reads_and_is_stored_as_before(store):
    agent = agents.create(store, configuration())
    assert "skillIds" not in agent["configuration"]
    with store._connect() as db:
        stored = db.execute(
            "SELECT configuration FROM agent_versions WHERE agent_id=?", (agent["id"],)
        ).fetchone()[0]
    assert "skillIds" not in stored
    assert agents.AgentInput.model_validate_json(stored).skillIds == []
    imported(store)
    with_skills = agents.create(store, configuration(["review"], name="With"))
    assert with_skills["configuration"]["skillIds"] == ["review"]


def test_the_model_text_says_who_attached_the_skill(store):
    imported(store, "review", "Agent checklist.")
    imported(store, "tone", "Be brief.")
    agent = agents.create(store, configuration(["review"]))
    sid = chat_with(store, agent)
    submissions.reserve(store, "text_source_request", sid, "Look", {}, skill_ids=["tone"])
    bound = submissions.bind(store, "text_source_request")
    execution = session_settings.execution(store, sid, turn_id=bound["turn_id"])
    texts = [message.content for message in skills.messages(execution)]
    assert texts[0].startswith("The owner attached this skill to the agent")
    assert texts[1].startswith("The owner explicitly selected this skill")
    assert all("does not grant tools" in text for text in texts)


def test_an_agent_conker_files_carries_no_skills(store):
    from pi.agents import agent_requests

    imported(store)
    with pytest.raises(ValueError):
        agent_requests._checked("agent", configuration(["review"]).model_dump())
    assert agent_requests._checked("agent", configuration().model_dump()).skillIds == []
