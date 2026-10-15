"""Carried immutable privacy protects new forks and legacy execution boundaries."""

import json
from contextlib import closing

import pytest

from pi.agents import profiles as agents
from pi.context import controls as context
from pi.conversation import settings, submissions
from pi.conversation.store import Store
from pi.memory import store as memory_store
from pi.memory.service import Memory
from pi.models.providers import Message
from pi.models.routing import Router
from pi.turns import context as turn_context
from pi.turns.loop import Loop

PUBLIC = {"memoryDisabled": False, "harnessDisabled": False}
MODES = [
    {"memoryDisabled": True, "harnessDisabled": False},
    {"memoryDisabled": False, "harnessDisabled": True},
    {"memoryDisabled": True, "harnessDisabled": True},
]


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "forks.db")) as value:
        yield value


def change(store, sid, privacy):
    return settings.save(
        store,
        sid,
        settings.Update(
            expected_revision=settings.load(store, sid)["revision"],
            settings=settings.Settings(agentId="companion", privacy=settings.Privacy(**privacy)),
        ),
    )


def source(store, sid):
    with store._connect() as db:
        return settings.source_privacy(db, sid)


def private_history(store, privacy):
    parent = store.create_session(privacy=privacy)
    pin = store.append_message(parent, "user", "An exact private source")
    change(store, parent, PUBLIC)
    return parent, pin


def carry(store, child, pin):
    # The old reviewed-fork code persisted the raw public settings before these refs.
    with store._connect() as db:
        db.execute("INSERT INTO context_inherited_messages VALUES(?,?,0)", (child, pin["id"]))


def legacy_child(store, privacy, *, attach=True):
    parent, pin = private_history(store, privacy)
    child = store.create_session(parent_id=parent, summary="A legacy reviewed summary")
    if attach:
        carry(store, child, pin)
    return parent, pin, child


def reviewed(store, sid, pins=(), request="reviewed_fork_privacy_001"):
    saved = context.load(store, sid)
    context.save(
        store,
        sid,
        context.Update(
            expected_revision=saved["revision"],
            policy=context.Policy(
                sessionInstructions="Keep exact context",
                messagePolicies={pin["id"]: "keep-exact" for pin in pins},
                budget=context.Budget(
                    contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                ),
            ),
        ),
    )
    messages = store.messages(sid)
    body = context.ReviewedFork(
        expected_revision=saved["revision"] + 1,
        expected_last_message_id=messages[-1]["id"] if messages else None,
        summary="Reviewed summary",
        request_id=request,
    )
    return context.reviewed_fork(store, sid, body), body


def raw(store, table, column, value):
    with store._connect() as db:
        return [
            tuple(row) for row in db.execute(f"SELECT * FROM {table} WHERE {column}=?", (value,))
        ]


@pytest.mark.parametrize("privacy", MODES)
def test_reviewed_private_pins_narrow_saved_settings_without_changing_source(store, privacy):
    parent, pin = private_history(store, privacy)
    original = raw(store, "message_privacy", "message_id", pin["id"])
    assert settings.execution(store, parent)["privacy"] == PUBLIC
    result, body = reviewed(store, parent, [pin])
    child = result["session_id"]
    saved = settings.load(store, child)
    assert saved["settings"]["privacy"] == privacy
    assert saved["revision"] == settings.load(store, parent)["revision"] == 2
    assert (
        json.loads(raw(store, "session_settings", "session_id", child)[0][2])["privacy"] == privacy
    )
    assert context.reviewed_fork(store, parent, body)["session_id"] == child
    assert context.history(store, child)[0]["id"] == pin["id"]
    assert store.messages(child) == []
    assert raw(store, "message_privacy", "message_id", pin["id"]) == original
    assert settings.execution(store, parent)["privacy"] == PUBLIC
    with pytest.raises(agents.AgentError) as error:
        change(store, child, PUBLIC)
    assert error.value.detail["code"] == "inherited_privacy"
    turn = store.start_turn(child)
    frozen = settings.execution(store, child, turn)
    assert frozen["privacy"] == frozen["settings"]["privacy"] == privacy
    message = store.append_message(child, "user", "Continue", turn_id=turn, purpose="input")
    assert raw(store, "message_privacy", "message_id", message["id"])[0][1:] == (
        int(privacy["memoryDisabled"]),
        int(privacy["harnessDisabled"]),
        int(not privacy["memoryDisabled"]),
    )
    assert (
        bool(raw(store, "memory_outbox", "message_id", message["id"]))
        is not privacy["memoryDisabled"]
    )


def test_public_pin_does_not_inherit_unselected_private_own_history(store):
    parent, private = private_history(store, MODES[2])
    public = store.append_message(parent, "user", "Public future input")
    result, _ = reviewed(store, parent, [public])
    child = result["session_id"]
    assert settings.execution(store, parent)["privacy"] == PUBLIC
    assert settings.execution(store, child)["privacy"] == PUBLIC
    assert source(store, parent)["incognito"] is True
    assert source(store, child)["incognito"] is False
    assert [row["id"] for row in context.history(store, child)] == [public["id"]]
    assert private["id"] != public["id"]


@pytest.mark.parametrize("privacy", MODES)
def test_legacy_child_reads_and_fresh_submission_are_protected_without_rewriting_rows(
    store, privacy
):
    _, pin, child = legacy_child(store, privacy)
    before = raw(store, "session_settings", "session_id", child)
    assert json.loads(before[0][2])["privacy"] == PUBLIC
    assert settings.load(store, child)["settings"]["privacy"] == privacy
    assert source(store, child) == {**privacy, "incognito": True}
    request = "legacy_protected_request_001"
    submissions.reserve(store, request, child, "Protected input", {})
    assert settings.execution(store, child, request_id=request)["privacy"] == privacy
    receipt = submissions.bind(store, request)
    assert settings.execution(store, child, receipt["turn_id"])["privacy"] == privacy
    assert raw(store, "session_settings", "session_id", child) == before
    assert raw(store, "message_privacy", "message_id", pin["id"])[0][1:3] == tuple(
        map(int, privacy.values())
    )


@pytest.mark.parametrize("kind", ["store", "reviewed", "automatic"])
@pytest.mark.parametrize("privacy", MODES)
def test_effective_parent_privacy_survives_summary_only_descendants(store, kind, privacy):
    _, _, parent = legacy_child(store, privacy)
    revision = settings.load(store, parent)["revision"]
    if kind == "store":
        child = store.create_session(parent_id=parent, summary="Owner summary")
    elif kind == "reviewed":
        result, _ = reviewed(store, parent)
        child = result["session_id"]
    else:
        request = "automatic_protected_request_001"
        submissions.reserve(store, request, parent, "First input", {})
        receipt = submissions.bind(store, request, fork_summary="Automatic summary")
        child = receipt["effective_session_id"]
        assert settings.execution(store, child, receipt["turn_id"])["privacy"] == privacy
        store.finish_turn(receipt["turn_id"], "complete")
    with store._connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM context_inherited_messages WHERE session_id=?", (child,)
            ).fetchone()[0]
            == 0
        )
    assert settings.load(store, child)["revision"] == revision
    assert (
        json.loads(raw(store, "session_settings", "session_id", child)[0][2])["privacy"] == privacy
    )
    assert settings.execution(store, child)["privacy"] == privacy
    turn = store.start_turn(child)
    assert settings.execution(store, child, turn)["privacy"] == privacy


def test_ordinary_public_parent_revision_and_ambient_defaults_are_unchanged(store):
    parent = store.create_session()
    child = store.create_session(parent_id=parent)
    assert settings.load(store, parent)["revision"] == settings.load(store, child)["revision"] == 0
    assert raw(store, "session_settings", "session_id", child) == []
    _, _, private_child = legacy_child(store, MODES[0])
    assert settings.load(store, private_child)["settings"]["privacy"] == MODES[0]
    change(store, parent, MODES[2])
    change(store, parent, PUBLIC)
    assert settings.execution(store, parent)["privacy"] == PUBLIC


@pytest.mark.parametrize("fork_summary", [None, "Must roll back new child"])
@pytest.mark.parametrize("privacy", MODES)
def test_unsafe_reserved_snapshot_refused_atomically_before_binding(store, fork_summary, privacy):
    _, pin, child = legacy_child(store, privacy, attach=False)
    request = "legacy_unsafe_request_001"
    submissions.reserve(store, request, child, "Retained pending input", {})
    before = {
        table: raw(store, table, "request_id", request)
        for table in ("turn_submissions", "submission_settings")
    }
    carry(store, child, pin)
    with pytest.raises(agents.AgentError) as error:
        settings.execution(store, child, request_id=request)
    assert error.value.detail["code"] == "inherited_privacy"
    with pytest.raises(agents.AgentError, match="predates inherited"):
        submissions.bind(store, request, fork_summary=fork_summary)
    assert len(store.list_sessions()) == 2
    assert store.get_session(child)["status"] == "open"
    assert store.messages(child) == store.turns(child) == []
    for table, content in before.items():
        assert raw(store, table, "request_id", request) == content


class NeverRemote:
    agent_id = "default"

    def retrieve(self, *args, **kwargs):
        pytest.fail("Unsafe inherited context reached memory retrieval")

    def deliver(self, *args, **kwargs):
        pytest.fail("Unsafe inherited context reached memory delivery")


class NeverProvider:
    name = "local"

    def complete(self, *args, **kwargs):
        pytest.fail("Unsafe inherited context reached inference")


@pytest.mark.parametrize("privacy", MODES)
def test_old_turn_recovery_recall_and_context_replay_refuse_without_journal_changes(store, privacy):
    _, pin, child = legacy_child(store, privacy, attach=False)
    completed = store.start_turn(child)
    turn_context.capture(store, completed, [], [Message("system", "Original completed context")])
    store.finish_turn(completed, "complete")
    turn = store.start_turn(child)
    turn_context.capture(store, turn, [], [Message("system", "Old derived recall")])
    store.finish_turn(turn, "acted_no_reply", acted=True)
    before = {
        (table, identity): raw(store, table, "turn_id", identity)
        for identity in (turn, completed)
        for table in ("turn_settings", "turn_context_inputs", "turn_context_policies")
    }
    before_turn = store.get_turn(turn)
    carry(store, child, pin)
    loop = Loop(
        store,
        Router(local_provider=NeverProvider(), local_model="test"),
        memory=Memory(store, NeverRemote()),
    )
    for action in (
        lambda: settings.execution(store, child, turn),
        lambda: loop.resume_turn(turn),
        lambda: loop.memory.prepare(turn, "query"),
        lambda: turn_context.replay(store, completed),
    ):
        with pytest.raises(agents.AgentError) as error:
            action()
        assert error.value.detail["code"] == "inherited_privacy"
    assert store.get_turn(turn) == before_turn
    for (table, identity), content in before.items():
        assert raw(store, table, "turn_id", identity) == content


@pytest.mark.parametrize("privacy", MODES)
def test_legacy_ingest_gate_preserves_harness_only_and_delete_semantics(store, privacy):
    _, pin, child = legacy_child(store, privacy, attach=False)
    message = store.append_message(child, "user", "Queued under the old public settings")
    flags = raw(store, "message_privacy", "message_id", message["id"])
    carry(store, child, pin)
    if privacy["memoryDisabled"]:
        before = raw(store, "memory_outbox", "message_id", message["id"])
        with pytest.raises(ValueError, match="exclude"):
            memory_store.pin_destination(store, message["id"], "default")
        assert raw(store, "memory_outbox", "message_id", message["id"]) == before
        assert Memory(store, NeverRemote()).drain_once() == 0
        with store._connect() as db:
            state = db.execute(
                "SELECT state,delivery_started FROM memory_outbox WHERE message_id=?",
                (message["id"],),
            ).fetchone()
            assert tuple(state) == ("blocked", 0)
    else:
        assert memory_store.pin_destination(store, message["id"], "default") == "default"
    assert raw(store, "message_privacy", "message_id", message["id"]) == flags
    with store._connect() as db:
        db.execute(
            "UPDATE memory_outbox SET operation='delete' WHERE message_id=?", (message["id"],)
        )
    assert memory_store.pin_destination(store, message["id"], "default") == "default"


def break_source(store, parent, pin, kind):
    with store._connect() as db:
        if kind == "missing_privacy":
            db.execute("DROP TRIGGER message_privacy_no_delete")
            db.execute("DELETE FROM message_privacy WHERE message_id=?", (pin["id"],))
        elif kind == "ledger":
            db.execute(
                "INSERT INTO forgetting_receipts VALUES(?,?,0,'confirmed',0,0)",
                (f"del_{parent}", parent),
            )
            db.execute("INSERT INTO forgotten_sessions VALUES(?,?)", (parent, f"del_{parent}"))
        elif kind == "status":
            db.execute("UPDATE sessions SET status='forgotten' WHERE id=?", (parent,))
        else:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute(
                "UPDATE context_inherited_messages SET message_id='missing' WHERE message_id=?",
                (pin["id"],),
            )


@pytest.mark.parametrize("kind", ["missing_privacy", "ledger", "status", "missing_message"])
def test_unknown_inherited_sources_fail_closed_without_remote_or_child_creation(store, kind):
    parent, pin, child = legacy_child(store, MODES[0], attach=False)
    queued = store.append_message(child, "user", "Legacy queued content")
    carry(store, child, pin)
    break_source(store, parent, pin, kind)
    assert source(store, child) is None
    with pytest.raises(agents.AgentError) as error:
        settings.load(store, child)
    assert error.value.detail["code"] == "inherited_source_unavailable"
    assert "Start a new conversation" in str(error.value)
    for action in (
        lambda: settings.execution(store, child),
        lambda: store.start_turn(child),
        lambda: store.create_session(parent_id=child, summary="Unknown origin"),
        lambda: submissions.reserve(store, "unknown_origin_request_001", child, "Question", {}),
    ):
        with pytest.raises(agents.AgentError, match="unavailable"):
            action()
    with pytest.raises(ValueError, match="privacy is unavailable"):
        memory_store.pin_destination(store, queued["id"], "default")
    with store._connect() as db:
        db.execute(
            "UPDATE memory_outbox SET operation='delete' WHERE message_id=?", (queued["id"],)
        )
    assert memory_store.pin_destination(store, queued["id"], "default") == "default"
    assert len(store.list_sessions()) == 2
    assert store.turns(child) == []
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM turn_submissions").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["missing_privacy", "ledger", "status"])
def test_reviewed_fork_unknown_pin_rolls_back_without_closing_source(store, kind):
    origin = store.create_session()
    pin = store.append_message(origin, "user", "Original public source")
    parent = store.create_session(parent_id=origin)
    carry(store, parent, pin)
    break_source(store, origin, pin, kind)
    with pytest.raises(context.ContextError, match="unavailable"):
        reviewed(store, parent, [pin])
    assert store.get_session(parent)["status"] == "open"
    assert len(store.list_sessions()) == 2
    with store._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM context_fork_requests").fetchone()[0] == 0


def test_frozen_nested_policy_cannot_disagree_with_inherited_floor(store):
    _, _, child = legacy_child(store, MODES[0])
    snapshot = settings.execution(store, child)
    snapshot["settings"]["privacy"] = PUBLIC.copy()
    with store._connect() as db, pytest.raises(agents.AgentError, match="predates inherited"):
        settings.require_inherited_privacy(db, child, snapshot)


@pytest.mark.parametrize("privacy", MODES)
def test_legacy_double_reviewed_fork_protects_summary_and_frozen_work(store, monkeypatch, privacy):
    origin, pin = private_history(store, privacy)
    # Reproduce both old reviewed-fork calls through the normal APIs. The old
    # implementation relied only on the raw settings inheritance trigger.
    with monkeypatch.context() as old:
        old.setattr(settings, "copy_parent_settings", lambda *_: None)
        old.setattr(settings, "inherited_privacy", lambda *_: PUBLIC.copy())
        first, _ = reviewed(store, origin, [pin], "old_first_reviewed_fork")
        second, _ = reviewed(store, first["session_id"], [], "old_second_reviewed_fork")
        child = second["session_id"]
        turn = store.start_turn(child)
        turn_context.capture(store, turn, [], [Message("system", "Legacy summary context")])
        store.finish_turn(turn, "complete")
        queued = store.append_message(child, "user", "Queued from old summary-only child")
        request = "old_summary_request_001"
        submissions.reserve(store, request, child, "Legacy saved input", {})
    with store._connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM context_inherited_messages WHERE session_id=?", (child,)
            ).fetchone()[0]
            == 0
        )
    before = {
        (table, key, value): raw(store, table, key, value)
        for table, key, value in (
            ("session_settings", "session_id", child),
            ("turn_settings", "turn_id", turn),
            ("turn_context_inputs", "turn_id", turn),
            ("submission_settings", "request_id", request),
            ("turn_submissions", "request_id", request),
            ("message_privacy", "message_id", queued["id"]),
        )
    }
    assert json.loads(before[("session_settings", "session_id", child)][0][2])["privacy"] == PUBLIC
    assert source(store, child) == {**privacy, "incognito": True}
    assert settings.execution(store, child)["privacy"] == privacy
    for action in (
        lambda: settings.execution(store, child, turn),
        lambda: settings.execution(store, child, request_id=request),
        lambda: turn_context.replay(store, turn),
        lambda: submissions.bind(store, request, fork_summary="Unsafe next descendant"),
        lambda: Memory(store, NeverRemote()).prepare(turn, "query"),
    ):
        with pytest.raises(agents.AgentError, match="predates inherited"):
            action()
    if privacy["memoryDisabled"]:
        with pytest.raises(ValueError, match="exclude"):
            memory_store.pin_destination(store, queued["id"], "default")
    else:
        assert memory_store.pin_destination(store, queued["id"], "default") == "default"
    assert len(store.list_sessions()) == 3
    assert store.get_session(child)["status"] == "open"
    for (table, key, value), content in before.items():
        assert raw(store, table, key, value) == content


def test_ordinary_ancestry_does_not_inherit_own_history_or_later_policy_changes(store):
    parent, _ = private_history(store, MODES[2])
    child = store.create_session(parent_id=parent, summary="Public owner-reviewed summary")
    grandchild = store.create_session(parent_id=child, summary="Another public summary")
    change(store, parent, MODES[2])
    change(store, child, MODES[0])
    assert settings.execution(store, grandchild)["privacy"] == PUBLIC
    assert source(store, grandchild) == {**PUBLIC, "incognito": False}
    change(store, grandchild, MODES[2])
    change(store, grandchild, PUBLIC)
    assert settings.execution(store, grandchild)["privacy"] == PUBLIC
