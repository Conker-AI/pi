"""A forgotten or trashed chat feeds no other chat: project sources, branches, team handoffs."""

import json
from contextlib import closing

import pytest
from test_loop import Recorder as LoopRecorder
from test_loop import loop_with
from test_project_context import setup as project_setup
from test_team_execution import Recorder as TeamRecorder
from test_team_execution import execute as team_execute
from test_team_execution import setup as team_setup
from test_team_execution import start as team_start

from pi.agents import profiles as agents
from pi.context import controls as context_controls
from pi.context import project_sources, projects
from pi.conversation import archive, forks, submissions
from pi.conversation.store import Store
from pi.schedule import tasks

REFUSED = (agents.AgentError, context_controls.ContextError)


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def trash(store, sessions, trashed=True):
    archive.trash(store, archive.Trash(sessions=sessions, trashed=trashed))


def resolver(store):
    return lambda reference: project_sources.resolve(store, reference)


def fork(store, sid, message_id, request_id, settings_revision=0):
    body = forks.Fork(
        request_id=request_id,
        expected_settings_revision=settings_revision,
        expected_context_revision=0,
    )
    return forks.create(store, sid, message_id, body)["session_id"]


def history_ids(store, sid):
    return [row["id"] for row in forks.branch_history(store, sid)["results"]]


# Project sources


def test_trashed_project_source_leaves_the_project_links_and_comes_back(store):
    origin, _, _, settings = project_setup(store)
    origin_title = "Synthetic trip notes 4471"
    with store._connect() as db:
        db.execute("UPDATE sessions SET title=? WHERE id=?", (origin_title, origin))
    trash(store, [origin])
    (link,) = projects.get(store, settings.projectId, resolver(store))["links"]
    assert link["availability"] == "unavailable" and link["label"] == "Unavailable reference"
    assert origin_title not in json.dumps(projects.get(store, settings.projectId, resolver(store)))
    assert projects.search(store, settings.projectId, "trip", resolver(store))["links"] == []
    reference = {"kind": "conversation", "sessionId": origin}
    assert project_sources.resolve(store, reference) is None
    trash(store, [origin], trashed=False)
    (link,) = projects.get(store, settings.projectId, resolver(store))["links"]
    assert link["availability"] == "available" and link["label"] == origin_title


def test_trashed_project_source_never_reaches_another_chats_model_and_restore_brings_it_back(
    store,
):
    origin, target, _, _ = project_setup(store)
    trash(store, [origin])
    provider = LoopRecorder()
    with pytest.raises(REFUSED, match="unavailable"):
        loop_with(store, provider).run_turn(target, "Use it", request_id="vis_project_trashed")
    assert provider.calls == []
    trash(store, [origin], trashed=False)
    loop_with(store, provider).run_turn(target, "Use it", request_id="vis_project_restored")
    assert any("Project evidence" in m.content for m in provider.calls[0])


def test_source_trashed_after_the_turn_was_frozen_is_refused_at_model_input(store):
    origin, target, _, _ = project_setup(store)
    request = "vis_project_frozen"
    submissions.reserve(store, request, target, "Read", {})
    turn = submissions.bind(store, request)["turn_id"]
    trash(store, [origin])
    with pytest.raises(REFUSED, match="unavailable"):
        loop_with(store, LoopRecorder())._history(target, turn_id=turn)
    trash(store, [origin], trashed=False)
    history = loop_with(store, LoopRecorder())._history(target, turn_id=turn)
    assert any("Project evidence" in m.content for m in history)


def test_trashed_task_and_file_sources_resolve_to_nothing(store):
    from pi.content import attachments
    from pi.conversation import settings as session_settings

    origin = store.create_session(title="Origin")
    turn = store.start_turn(origin)
    store.append_message(origin, "assistant", "Task output", turn_id=turn, purpose="final")
    task = tasks.create(
        store,
        tasks.CreateTask(
            request_id="vis_task_source_0001",
            session_id=origin,
            outcome="Research",
            criteria=["Done"],
            run_ids=[turn],
        ),
    )
    upload = attachments.upload(
        store,
        origin,
        attachments.Metadata(name="notes.txt", type="text/plain"),
        b"File words",
        resolve=session_settings.source_privacy,
    )
    references = [
        {"kind": "task", "taskId": task["id"]},
        {"kind": "file", "sessionId": origin, "fileId": upload["id"]},
    ]
    assert all(project_sources.resolve(store, ref) for ref in references)
    trash(store, [origin])
    assert [project_sources.resolve(store, ref) for ref in references] == [None, None]
    trash(store, [origin], trashed=False)
    assert all(project_sources.resolve(store, ref) for ref in references)


# Branches


def test_a_trashed_chat_cannot_be_branched_until_it_is_restored(store):
    sid = store.create_session()
    message = store.append_message(sid, "user", "Trashed words")
    trash(store, [sid])
    with pytest.raises(tasks.TaskError, match="Trash"):
        fork(store, sid, message["id"], "vis_fork_trashed_01")
    trash(store, [sid], trashed=False)
    child = fork(store, sid, message["id"], "vis_fork_restored_01")
    assert history_ids(store, child) == [message["id"]]


def test_branching_a_branch_of_a_trashed_chat_carries_none_of_its_words(store):
    parent = store.create_session()
    words = store.append_message(parent, "user", "Parent words")
    child = fork(store, parent, words["id"], "vis_fork_parent_01")
    own = store.append_message(child, "user", "Child words")
    trash(store, [parent])
    with pytest.raises(tasks.TaskError, match="Trash"):
        fork(store, child, own["id"], "vis_fork_grandchild", settings_revision=1)
    trash(store, [parent], trashed=False)
    grandchild = fork(store, child, own["id"], "vis_fork_grandchild", settings_revision=1)
    assert history_ids(store, grandchild) == [words["id"], own["id"]]


def test_branch_view_drops_a_hidden_parents_words_but_keeps_its_own(store):
    parent = store.create_session()
    words = store.append_message(parent, "user", "Parent words")
    child = fork(store, parent, words["id"], "vis_fork_view_0001")
    own = store.append_message(child, "user", "Child words")
    assert history_ids(store, child) == [words["id"], own["id"]]
    trash(store, [parent])
    page = forks.branch_history(store, child)
    assert [row["id"] for row in page["results"]] == [own["id"]]
    assert "Parent words" not in json.dumps(page)
    with pytest.raises(tasks.TaskError, match="cursor"):
        forks.branch_history(store, child, cursor=words["id"])
    trash(store, [parent], trashed=False)
    assert history_ids(store, child) == [words["id"], own["id"]]


def test_a_trashed_branch_opened_by_its_own_link_still_shows_everything(store):
    parent = store.create_session()
    words = store.append_message(parent, "user", "Parent words")
    child = fork(store, parent, words["id"], "vis_fork_own_00001")
    own = store.append_message(child, "user", "Child words")
    trash(store, [child])
    page = forks.branch_history(store, child)
    assert [(row["id"], row["inherited"]) for row in page["results"]] == [
        (words["id"], True),
        (own["id"], False),
    ]


# Team handoffs


def test_team_role_reads_nothing_from_a_trashed_source_chat(store):
    sid = store.create_session()
    chosen = store.append_message(sid, "user", "Selected evidence")
    record, _ = team_setup(store, [chosen["id"]])
    run = team_start(store, record, sid)
    trash(store, [sid])
    provider = TeamRecorder()
    with pytest.raises(agents.AgentError, match="Trash"):
        team_execute(store, provider, run)
    assert provider.calls == []
    trash(store, [sid], trashed=False)
    first = team_execute(store, provider, run)
    assert first["state"] == "ready"
    assert "Selected evidence" in provider.calls[0][1][-1].content


def test_team_cannot_start_with_messages_selected_from_a_trashed_chat(store):
    sid = store.create_session()
    chosen = store.append_message(sid, "user", "Selected evidence")
    record, _ = team_setup(store, [chosen["id"]])
    trash(store, [sid])
    with pytest.raises(agents.AgentError, match="available messages"):
        team_start(store, record, sid)
    trash(store, [sid], trashed=False)
    assert team_start(store, record, sid)["state"] == "ready"


def test_a_trashed_role_chat_is_not_handed_to_the_next_role(store):
    sid = store.create_session()
    record, _ = team_setup(store)
    run = team_start(store, record, sid)
    provider = TeamRecorder()
    first = team_execute(store, provider, run)
    role_chat = first["steps"][0]["session_id"]
    trash(store, [role_chat])
    with pytest.raises(agents.AgentError, match="Trash"):
        team_execute(store, provider, first, second=True)
    assert len(provider.calls) == 1
    trash(store, [role_chat], trashed=False)
    second = team_execute(store, provider, first, second=True)
    assert len(second["steps"]) == 2
    handed = json.loads(provider.calls[1][1][-1].content)["handoff"]
    assert handed["result"] == "role result"


def test_a_trashed_team_root_chat_hands_its_task_to_no_role(store):
    sid = store.create_session()
    record, _ = team_setup(store)
    run = team_start(store, record, sid, text="Synthetic owner task 9921")
    trash(store, [run["rootSessionId"]])
    provider = TeamRecorder()
    with pytest.raises(agents.AgentError, match="Trash"):
        team_execute(store, provider, run)
    assert provider.calls == []
