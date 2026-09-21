from contextlib import closing

import pytest

from pi import agents, projects, session_settings, submissions
from pi.loop import Loop
from pi.routing import Router
from pi.store import Store


def test_project_instructions_frozen_at_submission(tmp_path):
    with closing(Store(tmp_path / "project.db")) as store:
        sid = store.create_session()
        fields = projects.Fields(name="Research", description="", instructions="Cite sources.")
        project = projects.create(store, fields)
        session_settings.save(
            store,
            sid,
            session_settings.Update(
                expected_revision=0,
                settings=session_settings.Settings(
                    agentId="companion",
                    projectId=project["id"],
                    privacy=session_settings.Privacy(memoryDisabled=False, harnessDisabled=False),
                ),
            ),
        )
        request = "project_context_request"
        submissions.reserve(store, request, sid, "Question", {})
        projects.mutate(
            store,
            project["id"],
            projects.Update(
                expected_revision=1,
                fields=fields.model_copy(update={"instructions": "New instruction"}),
            ),
            "update",
        )
        bound = submissions.bind(store, request)
        execution = session_settings.execution(store, sid, turn_id=bound["turn_id"])
        assert execution["project"]["revision"] == 1
        loop = Loop(store, Router(local_provider=None, local_model="unused"))
        messages = loop._history(sid, turn_id=bound["turn_id"])
        assert any(message.content == "Cite sources." for message in messages)
        assert not any(message.content == "New instruction" for message in messages)
        assert execution["authority"] == "none"


def test_unknown_project_rejected_before_settings_are_saved(tmp_path):
    with closing(Store(tmp_path / "project.db")) as store:
        sid = store.create_session()
        with pytest.raises(agents.AgentError, match="active project"):
            session_settings.save(
                store,
                sid,
                session_settings.Update(
                    expected_revision=0,
                    settings=session_settings.Settings(
                        agentId="companion",
                        projectId="missing",
                        privacy=session_settings.Privacy(
                            memoryDisabled=False, harnessDisabled=False
                        ),
                    ),
                ),
            )
        assert session_settings.load(store, sid)["revision"] == 0
