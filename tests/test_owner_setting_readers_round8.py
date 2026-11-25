"""Owner settings for search, new chats and project files; each test fails if its reader ignores
the value."""

from contextlib import closing

import pytest
from test_attachment_turns import Provider, upload

from pi.context import projects, universal_search
from pi.conversation import archive, drafts
from pi.conversation import search as conversation_search
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.core import owner_settings as owner_settings
from pi.models.routing import Router
from pi.turns.loop import Loop


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def choose(store, values):
    revision = owner_settings.load(store)["revision"]
    owner_settings.save(store, owner_settings.Update(expected_revision=revision, values=values))


# search.archived


def found(store):
    words = [row["session_id"] for row in conversation_search.search(store, "tent")["results"]]
    titles = [row["recordId"] for row in universal_search.conversation_metadata(store, "trip")[0]]
    return set(words), set(titles)


def test_archived_chats_are_searched_only_when_the_owner_includes_them(store):
    kept, archived = (store.create_session(title=f"trip {n}") for n in ("kept", "archived"))
    for sid in (kept, archived):
        store.append_message(sid, "user", "pack the tent")
    archive.apply(store, archive.Archive(sessions=[archived], archived=True))
    assert found(store) == ({kept}, {kept})
    choose(store, {"search.archived": "yes"})  # only an exact true includes them
    assert found(store) == ({kept}, {kept})
    choose(store, {"search.archived": True})
    assert found(store) == ({kept, archived}, {kept, archived})


# research.default


def start(store, text="hello"):
    draft = drafts.load_new(store)
    saved = drafts.save_new(
        store,
        drafts.NewSave(
            expected_revision=draft["revision"], text=text, research_mode=draft["research_mode"]
        ),
    )
    request = f"start_request_{saved['revision']:08d}"
    started = drafts.start(
        store, drafts.Start(request_id=request, expected_revision=saved["revision"])
    )
    return drafts.load(store, started["session_id"])


def test_new_chats_start_in_the_owners_research_mode(store):
    assert drafts.load_new(store)["research_mode"] == "off"
    choose(store, {"research.default": "web"})
    assert drafts.load_new(store)["research_mode"] == "web"
    assert start(store)["research_mode"] == "web"
    # The next new chat reads the setting again, as it is now.
    choose(store, {"research.default": "deep"})
    assert drafts.load_new(store)["research_mode"] == "deep"
    choose(store, {"research.default": "everything"})
    assert drafts.load_new(store)["research_mode"] == "off"


def test_a_mode_picked_for_the_new_chat_wins_over_the_default(store):
    choose(store, {"research.default": "web"})
    draft = drafts.load_new(store)
    drafts.save_new(
        store, drafts.NewSave(expected_revision=draft["revision"], text="hi", research_mode="off")
    )
    assert drafts.load_new(store)["research_mode"] == "off"


# files.to_project


def project_chat(store, private=False):
    project = projects.create(store, projects.Fields(name="Trip", description="", instructions=""))
    sid = store.create_session()
    settings = session_settings.Settings(
        agentId="companion",
        projectId=project["id"],
        privacy=session_settings.Privacy(memoryDisabled=private, harnessDisabled=False),
    )
    session_settings.save(
        store, sid, session_settings.Update(expected_revision=0, settings=settings)
    )
    return project["id"], sid


def send_file(store, sid, request="project_file_request_1"):
    identity = upload(store, sid)
    loop = Loop(store, Router(local_provider=Provider(), local_model="local"))
    loop.run_turn(sid, "Read this", request_id=request, attachment_ids=[identity])
    [sent] = [m for m in store.messages(sid) if m["role"] == "user"]
    assert [item["id"] for item in sent["attachments"]] == [identity]
    return identity


def linked(store, project_id):
    return [link["reference"] for link in projects.get(store, project_id)["links"]]


def test_a_file_sent_in_a_project_chat_joins_the_project(store):
    project_id, sid = project_chat(store)
    revision = projects.get(store, project_id)["revision"]
    identity = send_file(store, sid)
    assert linked(store, project_id) == [{"kind": "file", "sessionId": sid, "fileId": identity}]
    assert projects.get(store, project_id)["revision"] == revision + 1
    # An upload that is never sent does not join.
    upload(store, sid, name="draft.txt")
    assert len(linked(store, project_id)) == 1


def test_files_stay_in_the_chat_when_the_owner_turns_it_off(store):
    choose(store, {"files.to_project": False})
    project_id, sid = project_chat(store)
    send_file(store, sid)
    assert linked(store, project_id) == []


def test_a_private_chats_files_never_join(store):
    project_id, sid = project_chat(store, private=True)
    send_file(store, sid)
    assert linked(store, project_id) == []
