"""The companion remembers across chats by default; his switch in Settings keeps each chat to itself."""

import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from pi.agents import profiles as agents
from pi.agents import teams
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.memory import service
from pi.memory import store as memory_store
from pi.owner import settings as owner_settings

RECALL = {"max_items": 8, "distinct": True}
EVERY_CHAT = {"scope": "all", "session_id": None, "memory_ids": [], **RECALL}


class Client:
    def __init__(self):
        self.calls = []

    def retrieve(self, query, **options):
        self.calls.append(options)
        return {"memories": [], "retrieval": {"semantic": {"status": "ok"}}}


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def recall(store, monkeypatch, sid, selected=None):
    """What one companion turn in this chat asks MemoryGate for."""
    selected = selected or session_settings.execution(store, sid)
    monkeypatch.setattr(store, "get_turn", lambda identity: {"session_id": sid})
    monkeypatch.setattr(session_settings, "execution", lambda *args: selected)
    monkeypatch.setattr(memory_store, "recall_held", lambda *args: 0)
    monkeypatch.setattr(memory_store, "save_context", lambda *args: None)
    client = Client()
    service.Memory(store, client).prepare("turn", "When does my study group meet?")
    return client.calls


def test_a_new_chat_reads_memories_from_every_chat(store, monkeypatch):
    assert agents.get(store, "companion")["configuration"]["memory"]["scope"] == "owner"
    assert recall(store, monkeypatch, store.create_session()) == [EVERY_CHAT]


def test_saving_a_chat_setting_keeps_recall_across_chats(store, monkeypatch):
    sid = store.create_session()
    session_settings.save(
        store,
        sid,
        session_settings.Update(
            expected_revision=0,
            settings=session_settings.Settings(
                agentId="companion",
                privacy=session_settings.Privacy(memoryDisabled=False, harnessDisabled=False),
            ),
        ),
    )
    assert session_settings.load(store, sid)["revision"] == 1
    assert recall(store, monkeypatch, sid) == [EVERY_CHAT]


# On only when he never set it or set it to exactly true: a broken value keeps chats apart.
@pytest.mark.parametrize(
    "value,across",
    [(False, False), (True, True), ("off", False), ("true", False), (1, False), (None, False)],
)
def test_his_switch_keeps_each_chat_to_itself(store, monkeypatch, value, across):
    owner_settings.save(
        store, owner_settings.Update(expected_revision=0, values={service.ACROSS_CHATS: value})
    )
    sid = store.create_session()
    this_chat = {"scope": "conversation", "session_id": sid, "memory_ids": [], **RECALL}
    assert recall(store, monkeypatch, sid) == [EVERY_CHAT if across else this_chat]


def test_turns_from_before_agent_profiles_use_the_companion_default(store, monkeypatch):
    sid = store.create_session()
    legacy = {
        "agentId": "companion",
        "kind": "companion",
        "configuration": None,
        "privacy": {"memoryDisabled": False, "harnessDisabled": False},
        "legacy": True,
    }
    assert recall(store, monkeypatch, sid, legacy) == [EVERY_CHAT]


def old_install(tmp_path, *saved_scopes):
    """A Pi database whose companion was created reading only this chat, then saved as given."""
    db = sqlite3.connect(tmp_path / "old.db", isolation_level=None)
    db.row_factory = sqlite3.Row
    db.executescript(agents.SCHEMA)
    configuration = agents.AgentInput(
        name="Conker",
        role="The daily companion",
        instructions="Act as the owner's daily companion.",
        modelId=None,
        toolIds=[],
        memory=agents.MemorySelection(scope="conversation", memoryIds=[]),
    )
    agents._insert(db, "companion", "companion", configuration, 1.0)
    for scope in saved_scopes:
        changed = agents.AgentInput.model_validate(
            {**configuration.model_dump(), "memory": {"scope": scope, "memoryIds": []}}
        )
        agents._append(db, agents._get(db, "companion"), changed, None, "updated")
    return db


def test_an_old_companion_he_never_edited_moves_once_and_keeps_its_history(tmp_path):
    with closing(old_install(tmp_path)) as db:
        agents.initialize(db)
        agents.initialize(db)
        moved = agents._get(db, "companion")
        assert moved["revision"] == 2 and moved["change_kind"] == "updated"
        assert moved["configuration"]["memory"]["scope"] == "owner"
        first = agents._get(db, "companion", revision=1)
        assert first["configuration"]["memory"]["scope"] == "conversation"


def test_a_companion_he_saved_keeps_his_choice(tmp_path):
    with closing(old_install(tmp_path, "conversation")) as db:
        agents.initialize(db)
        kept = agents._get(db, "companion")
        assert kept["revision"] == 2
        assert kept["configuration"]["memory"]["scope"] == "conversation"


@pytest.mark.parametrize(
    "scope,allowed", [("conversation", True), ("none", True), ("selected", False)]
)
def test_a_team_role_may_read_less_than_the_companion(store, scope, allowed):
    role = SimpleNamespace(
        id="first",
        agentId="companion",
        toolIds=[],
        memory=SimpleNamespace(scope=scope, memoryIds=["memory_a"] if scope == "selected" else []),
    )
    with store._connect() as db:
        if allowed:
            assert teams._team_agents(db, SimpleNamespace(roles=[role]))[0]["roleId"] == "first"
        else:
            with pytest.raises(agents.AgentError, match="Role memory exceeds"):
                teams._team_agents(db, SimpleNamespace(roles=[role]))
