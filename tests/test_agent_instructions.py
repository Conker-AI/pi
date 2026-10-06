"""Saved agent instructions reach the model without changing execution authority."""

from contextlib import closing

import pytest
from test_loop import Recorder, loop_with

from pi import agents, session_settings, submissions, turn_context
from pi.loop import Loop
from pi.routing import Router
from pi.store import Store


@pytest.mark.parametrize("kind", ["companion", "agent"])
def test_agent_instructions_frozen_at_submission(tmp_path, kind):
    with closing(Store(tmp_path / "agent.db")) as store:
        original = agents.AgentInput(
            name="Planning assistant",
            role="Plan with the owner",
            instructions="Report conflicts before suggesting a rehearsal time.",
            modelId=None,
            toolIds=[],
            memory=agents.MemorySelection(scope="conversation", memoryIds=[]),
        )
        if kind == "companion":
            agent = agents.update(
                store,
                "companion",
                agents.UpdateAgent(expected_revision=1, configuration=original),
            )
        else:
            agent = agents.create(store, original)
        sid = store.create_session()
        session_settings.save(
            store,
            sid,
            session_settings.Update(
                expected_revision=0,
                settings=session_settings.Settings(
                    agentId=agent["id"],
                    privacy=session_settings.Privacy(memoryDisabled=False, harnessDisabled=False),
                ),
            ),
        )
        request = "agent_instruction_request"
        submissions.reserve(store, request, sid, "Plan today's rehearsal.", {})
        changed = original.model_copy(update={"instructions": "Changed after submission."})
        agents.update(
            store,
            agent["id"],
            agents.UpdateAgent(expected_revision=agent["revision"], configuration=changed),
        )
        bound = submissions.bind(store, request)
        execution = session_settings.execution(store, sid, turn_id=bound["turn_id"])
        loop = Loop(
            store,
            Router(local_provider=None, local_model="unused"),
            system_prompt="Operator instruction.",
        )
        messages = loop._history(sid, turn_id=bound["turn_id"])
        assert messages[0].content == "Operator instruction."
        supplied = [message for message in messages if message.content == original.instructions]
        assert len(supplied) == 1 and supplied[0].role == "system"
        assert all(message.content != changed.instructions for message in messages)
        assert execution["agentVersion"] == agent["revision"]
        assert execution["authority"] == "none"
        assert execution["configuration"]["toolIds"] == []
        assert execution["configuration"]["memory"]["scope"] == "conversation"
        assert any(
            item["content"] == original.instructions
            for item in turn_context.load(store, bound["turn_id"])["prefix"]
        )
        assert any(message.content == changed.instructions for message in loop._history(sid))


def test_actual_companion_answer_receives_frozen_instructions(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "answer.db")) as store:
        profile = agents.AgentInput.model_validate(agents.get(store, "companion")["configuration"])
        profile.instructions = "Explain schedule conflicts before suggesting times."
        agents.update(
            store, "companion", agents.UpdateAgent(expected_revision=1, configuration=profile)
        )
        sid = store.create_session()
        provider = Recorder()
        loop = loop_with(store, provider)
        original_run = loop._run_bound

        def edit_then_run(*args, **kwargs):
            agents.update(
                store,
                "companion",
                agents.UpdateAgent(
                    expected_revision=2,
                    configuration=profile.model_copy(update={"instructions": "Later instruction."}),
                ),
            )
            return original_run(*args, **kwargs)

        monkeypatch.setattr(loop, "_run_bound", edit_then_run)
        result = loop.run_turn(sid, "Plan today.", request_id="companion_frozen_answer")
        assert any(message.content == profile.instructions for message in provider.calls[0])
        assert all(message.content != "Later instruction." for message in provider.calls[0])
        assert turn_context.replay(store, result["turn_id"])["messages"] == provider.calls[0]
        monkeypatch.setattr(loop, "_run_bound", original_run)
        loop.run_turn(sid, "Plan tomorrow.", request_id="companion_updated_answer")
        assert any(message.content == "Later instruction." for message in provider.calls[1])


def test_legacy_turn_does_not_invent_current_companion_instructions(tmp_path):
    with closing(Store(tmp_path / "legacy.db")) as store:
        sid = store.create_session()
        turn = "trn_legacy_instructions"
        # Legacy turns predate immutable execution snapshots.
        with store._connect() as db:
            db.execute(
                "INSERT INTO turns(id,session_id,status,started_at) VALUES (?,?,'running',0)",
                (turn, sid),
            )
        store.append_message(sid, "user", "Legacy request", turn_id=turn, purpose="input")
        companion = agents.get(store, "companion")
        loop = Loop(store, Router(local_provider=None, local_model="unused"))
        messages = loop._history(sid, turn_id=turn)
        assert session_settings.execution(store, sid, turn_id=turn)["legacy"] is True
        assert all(
            message.content != companion["configuration"]["instructions"] for message in messages
        )
