from contextlib import closing

import pytest
from test_loop import Recorder, loop_with

from pi import (
    agents,
    attachments,
    forgetting,
    project_sources,
    projects,
    session_settings,
    submissions,
    tasks,
)
from pi.store import Store


def setup(store, text="Project evidence"):
    origin, target = store.create_session(), store.create_session()
    message = store.append_message(origin, "user", text)
    project = projects.create(
        store, projects.Fields(name="Research", description="", instructions="")
    )
    reference = {"kind": "conversation", "sessionId": origin}
    projects.mutate(
        store,
        project["id"],
        projects.Link(expected_revision=1, reference=reference),
        "link",
        lambda ref: project_sources.resolve(store, ref),
    )
    settings = session_settings.Settings(
        agentId="companion",
        projectId=project["id"],
        projectSources=[reference],
        privacy=session_settings.Privacy(memoryDisabled=False, harnessDisabled=False),
    )
    session_settings.save(
        store, target, session_settings.Update(expected_revision=0, settings=settings)
    )
    return origin, target, message, settings


def test_actual_turn_uses_selected_project_text_without_copying_source(tmp_path):
    with closing(Store(tmp_path / "project.db")) as store:
        _, target, message, _ = setup(store)
        provider = Recorder()
        result = loop_with(store, provider).run_turn(
            target, "Use evidence", request_id="project_actual_turn"
        )
        context = next(m for m in provider.calls[0] if "Untrusted project reference" in m.content)
        assert context.role == "user" and message["id"] in context.content
        assert "Project evidence" in context.content
        assert [m["content"] for m in store.messages(target)] == ["Use evidence", "answered"]
        snap = session_settings.execution(store, target, result["turn_id"])
        assert snap["projectContext"][0]["messageIds"] == [message["id"]]
        assert "Project evidence" not in str(snap)


def test_snapshot_does_not_gain_later_messages_or_new_link_configuration(tmp_path):
    with closing(Store(tmp_path / "project.db")) as store:
        origin, target, _, _ = setup(store)
        request = "project_frozen_context"
        submissions.reserve(store, request, target, "Question", {})
        store.append_message(origin, "user", "Later source message")
        turn = submissions.bind(store, request)["turn_id"]
        history = loop_with(store, Recorder())._history(target, turn_id=turn)
        text = "\n".join(m.content for m in history)
        assert "Project evidence" in text and "Later source message" not in text


def test_selecting_project_alone_does_not_import_its_links(tmp_path):
    with closing(Store(tmp_path / "project.db")) as store:
        _, target, _, settings = setup(store)
        settings.projectSources = []
        session_settings.save(
            store, target, session_settings.Update(expected_revision=1, settings=settings)
        )
        provider = Recorder()
        loop_with(store, provider).run_turn(target, "Hello", request_id="project_no_source_list")
        assert all("Project evidence" not in m.content for m in provider.calls[0])


def test_private_or_oversized_sources_block_before_provider_and_leave_no_reservation(tmp_path):
    with closing(Store(tmp_path / "project.db")) as store:
        origin, target, _, _ = setup(store, "x" * 16000)
        store.append_message(origin, "user", "y" * 16000)
        provider = Recorder()
        with pytest.raises(agents.AgentError, match="32000"):
            loop_with(store, provider).run_turn(target, "Hello", request_id="project_size_rejected")
        with store._connect() as db:
            assert (
                db.execute("SELECT count(*) FROM project_context_dependencies").fetchone()[0] == 0
            )
            assert db.execute("SELECT count(*) FROM turn_submissions").fetchone()[0] == 0
        session_settings.save(
            store,
            origin,
            session_settings.Update(
                expected_revision=0,
                settings=session_settings.Settings(
                    agentId="companion",
                    privacy=session_settings.Privacy(memoryDisabled=True, harnessDisabled=False),
                ),
            ),
        )
        with pytest.raises(agents.AgentError, match="Private sources"):
            loop_with(store, provider).run_turn(
                target, "Hello", request_id="project_private_rejected"
            )
        assert provider.calls == []


def test_read_only_inspection_has_no_dependencies_and_forgetting_cascades_after_use(tmp_path):
    path = tmp_path / "project.db"
    secret = "source-private-evidence-651773"
    with closing(Store(path)) as store:
        origin, target, _, _ = setup(store, secret)
        session_settings.execution(store, target)
        with store._connect() as db:
            assert (
                db.execute("SELECT count(*) FROM project_context_dependencies").fetchone()[0] == 0
            )
        loop_with(store, Recorder(secret)).run_turn(
            target, "Use source", request_id="project_forget_source"
        )
    preview = forgetting.preview(path, origin)
    assert set(preview["session_ids"]) == {origin, target}
    forgetting.forget(path, origin, preview["confirmation"])
    with closing(Store(path)) as store:
        assert store.get_session(target)["status"] == "forgotten"
    for file in tmp_path.iterdir():
        if file.is_file():
            assert secret.encode() not in file.read_bytes()


def test_selected_file_is_bound_to_actual_link_and_plaintext(tmp_path):
    with closing(Store(tmp_path / "project.db")) as store:
        origin, target, _, settings = setup(store)
        upload = attachments.upload(
            store,
            origin,
            attachments.Metadata(name="evidence.txt", type="text/plain"),
            b"File evidence",
            resolve=session_settings.source_privacy,
        )
        reference = {"kind": "file", "sessionId": origin, "fileId": upload["id"]}
        settings = session_settings.Settings.model_validate(
            {**settings.model_dump(), "projectSources": [reference]}
        )
        session_settings.save(
            store, target, session_settings.Update(expected_revision=1, settings=settings)
        )
        provider = Recorder()
        with pytest.raises(agents.AgentError, match="linked"):
            loop_with(store, provider).run_turn(target, "Read", request_id="project_unlinked_file")
        projects.mutate(
            store,
            settings.projectId,
            projects.Link(expected_revision=2, reference=reference),
            "link",
            lambda ref: project_sources.resolve(store, ref),
        )
        loop_with(store, provider).run_turn(target, "Read", request_id="project_linked_file_1")
        assert any(
            "File evidence" in m.content and upload["id"] in m.content for m in provider.calls[0]
        )


def test_source_becoming_private_blocks_frozen_context_read(tmp_path):
    with closing(Store(tmp_path / "project.db")) as store:
        origin, target, _, _ = setup(store)
        request = "project_privacy_changed"
        submissions.reserve(store, request, target, "Read", {})
        turn = submissions.bind(store, request)["turn_id"]
        session_settings.save(
            store,
            origin,
            session_settings.Update(
                expected_revision=0,
                settings=session_settings.Settings(
                    agentId="companion",
                    privacy=session_settings.Privacy(memoryDisabled=False, harnessDisabled=True),
                ),
            ),
        )
        from pi.context_controls import ContextError

        with pytest.raises(ContextError, match="Private sources"):
            loop_with(store, Recorder())._history(target, turn_id=turn)


def test_task_source_includes_only_messages_from_its_linked_runs(tmp_path):
    with closing(Store(tmp_path / "project.db")) as store:
        origin, target, _, settings = setup(store, "Unrelated conversation text")
        turn = store.start_turn(origin)
        store.append_message(
            origin, "assistant", "Task output evidence", turn_id=turn, purpose="final"
        )
        task = tasks.create(
            store,
            tasks.CreateTask(
                request_id="project_linked_task",
                session_id=origin,
                outcome="Research task",
                criteria=["Done"],
                run_ids=[turn],
            ),
        )
        reference = {"kind": "task", "taskId": task["id"]}
        projects.mutate(
            store,
            settings.projectId,
            projects.Link(expected_revision=2, reference=reference),
            "link",
            lambda ref: project_sources.resolve(store, ref),
        )
        settings = session_settings.Settings.model_validate(
            {**settings.model_dump(), "projectSources": [reference]}
        )
        session_settings.save(
            store, target, session_settings.Update(expected_revision=1, settings=settings)
        )
        provider = Recorder()
        loop_with(store, provider).run_turn(target, "Use task", request_id="project_task_context")
        text = "\n".join(m.content for m in provider.calls[0])
        assert "Task output evidence" in text and "Unrelated conversation text" not in text
