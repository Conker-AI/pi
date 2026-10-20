"""Reviewed-fork lineage limits: bounded, single-snapshot, fail-closed lineage reads."""

import pytest

from pi.agents import profiles as agents
from pi.agents import team_models
from pi.calls import call_models
from pi.conversation import settings, submissions
from pi.models.routing import Router
from pi.turns.loop import Loop
from tests.test_reviewed_fork_privacy import (
    MODES,
    PUBLIC,
    NeverProvider,
    break_source,
    legacy_child,
    raw,
    reviewed,
    source,
    store,  # noqa: F401 - pytest fixture used by name
)


@pytest.mark.parametrize("fault", ["missing_parent", "cycle", "depth"])
def test_invalid_lineage_is_bounded_and_never_guesses_public(store, fault):
    parent = store.create_session()
    child = store.create_session(parent_id=parent)
    with store._connect() as db:
        if fault == "missing_parent":
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("UPDATE sessions SET parent_id='missing' WHERE id=?", (parent,))
        elif fault == "cycle":
            db.execute("UPDATE sessions SET parent_id=? WHERE id=?", (child, parent))
        else:
            for index in range(settings.MAX_PRIVACY_LINEAGE - 2):
                next_id = f"ses_lineage_{index}"
                db.execute(
                    "INSERT INTO sessions(id,parent_id,created_at) VALUES(?,?,0)", (next_id, child)
                )
                child = next_id
            assert settings.inherited_privacy(db, child) == PUBLIC
            db.execute(
                "INSERT INTO sessions(id,parent_id,created_at) VALUES('ses_too_deep',?,0)", (child,)
            )
            child = "ses_too_deep"
        statements = []
        db.set_trace_callback(statements.append)
        with pytest.raises(agents.AgentError) as error:
            settings.inherited_privacy(db, child)
        assert error.value.detail["code"] == "inherited_source_unavailable"
        assert (
            len([sql for sql in statements if sql.startswith("SELECT ancestor")])
            <= settings.MAX_PRIVACY_LINEAGE
        )
        assert db.in_transaction is False
    assert source(store, child) is None
    with pytest.raises(agents.AgentError):
        store.start_turn(child)
    assert store.turns(child) == []


def test_lineage_read_uses_one_snapshot_and_leaves_no_transaction_or_data_changes(store):
    origin, pin, parent = legacy_child(store, MODES[0])
    child = store.create_session(parent_id=parent, summary="Derived summary")
    changed = False
    with store._connect() as db:
        before = db.total_changes

        def concurrent_forget(sql):
            nonlocal changed
            if (
                not changed
                and sql.startswith("SELECT ancestor")
                and f"ancestor.id='{parent}'" in sql
            ):
                changed = True
                break_source(store, origin, pin, "ledger")

        db.set_trace_callback(concurrent_forget)
        assert settings.inherited_privacy(db, child) == MODES[0]
        assert changed is True
        assert db.in_transaction is False and db.total_changes == before
        db.set_trace_callback(None)
        with pytest.raises(agents.AgentError, match="unavailable"):
            settings.inherited_privacy(db, child)
        assert db.in_transaction is False and db.total_changes == before


@pytest.mark.parametrize(
    "kind", ["store", "reviewed", "automatic", "message", "call", "team_start", "team_step"]
)
def test_creation_at_lineage_limit_refuses_atomically_even_with_inherited_raw_settings(store, kind):
    from pi.agents import team_execution
    from pi.calls import runtime as calls
    from pi.conversation import forks as message_forks

    parent = store.create_session(privacy=PUBLIC)
    with store._connect() as db:
        for index in range(settings.MAX_PRIVACY_LINEAGE - (2 if kind == "team_step" else 1)):
            child = f"ses_creation_depth_{index}"
            db.execute(
                "INSERT INTO sessions(id,parent_id,created_at) VALUES(?,?,0)", (child, parent)
            )
            parent = child
    assert settings.execution(store, parent)["privacy"] == PUBLIC
    assert raw(store, "session_settings", "session_id", parent)
    request = "max_depth_auto_request_001"
    before = None
    if kind == "automatic":
        submissions.reserve(store, request, parent, "Retained pending text", {})
        before = raw(store, "turn_submissions", "request_id", request)
    if kind == "message":
        boundary = store.append_message(parent, "user", "Exact branch boundary")
    if kind.startswith("team"):
        from test_team_execution import setup, start

        team, _ = setup(store)
        if kind == "team_step":
            run = start(store, team, parent)
    with store._connect() as db:
        counts = {
            table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "sessions",
                "messages",
                "calls",
                "call_events",
                "team_runs",
                "team_steps",
                "tasks",
                "message_fork_requests",
            )
        }
    with pytest.raises(agents.AgentError) as error:
        if kind == "store":
            store.create_session(parent_id=parent, summary="Too deep")
        elif kind == "reviewed":
            reviewed(store, parent)
        elif kind == "automatic":
            submissions.bind(store, request, fork_summary="Too deep")
        elif kind == "message":
            message_forks.create(
                store,
                parent,
                boundary["id"],
                message_forks.Fork(
                    request_id="too_deep_message_fork",
                    expected_settings_revision=settings.load(store, parent)["revision"],
                    expected_context_revision=0,
                ),
            )
        elif kind == "call":
            calls.start(
                store, call_models.Start(request_id="too_deep_call_start", conversationId=parent)
            )
        elif kind == "team_start":
            start(store, team, parent)
        else:
            team_execution.execute(
                store,
                Loop(store, Router(local_provider=NeverProvider(), local_model="test")),
                run["id"],
                team_models.Step(
                    request_id="too_deep_team_step",
                    expected_revision=run["revision"],
                    roleId="first",
                ),
            )
    assert error.value.detail["code"] == "inherited_source_unavailable"
    assert store.get_session(parent)["status"] == "open"
    assert store.turns(parent) == []
    with store._connect() as db:
        for table, count in counts.items():
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count
        assert db.execute("SELECT COUNT(*) FROM context_fork_requests").fetchone()[0] == 0
    if before is not None:
        assert raw(store, "turn_submissions", "request_id", request) == before
    if kind == "team_step":
        assert team_execution.get(store, run["id"]) == run
