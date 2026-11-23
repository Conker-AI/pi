"""Reconciling a team run after the owner's approval (hunt 3: team-skills findings 1, 3, 4)."""

import pytest

from pi.agents import team_execution as teams
from tests.team_execution_helpers import Recorder, execute, setup, start, store  # noqa: F401


def _after_first_step(store):
    sid = store.create_session()
    record, _ = setup(store)
    run = start(store, record, sid)
    return execute(store, Recorder(), run)


def _hold(store, run_id, turn_status):
    """The run as it stands once the owner approved a held step and its turn is `turn_status`."""
    with store._connect() as db:
        step = db.execute(
            "SELECT id, request_id FROM team_steps WHERE run_id=?", (run_id,)
        ).fetchone()
        turn = db.execute(
            "SELECT turn_id FROM turn_submissions WHERE request_id=?", (step["request_id"],)
        ).fetchone()
        db.execute("UPDATE team_steps SET state='held' WHERE id=?", (step["id"],))
        db.execute("UPDATE team_runs SET state='held' WHERE id=?", (run_id,))
        db.execute("UPDATE turns SET status=? WHERE id=?", (turn_status, turn["turn_id"]))


@pytest.mark.parametrize("turn_status", ["running", "preparing"])
def test_a_held_run_whose_resumed_turn_is_running_is_left_alone(store, turn_status):
    run = _after_first_step(store)
    _hold(store, run["id"], turn_status)
    before = teams.get(store, run["id"])
    after = teams.reconcile(store, run["id"])
    assert after["state"] == "held" and after["revision"] == before["revision"]
    assert [step["state"] for step in after["steps"]] == ["held"]


def test_a_held_run_moves_on_once_its_resumed_turn_has_completed(store):
    run = _after_first_step(store)
    _hold(store, run["id"], "complete")
    assert teams.reconcile(store, run["id"])["state"] == "ready"


def test_a_held_run_still_waiting_for_approval_stays_held(store):
    run = _after_first_step(store)
    _hold(store, run["id"], "awaiting_approval")
    assert teams.reconcile(store, run["id"])["state"] == "held"


def test_reconcile_without_a_change_does_not_bump_the_revision(store):
    run = _after_first_step(store)
    first = teams.reconcile(store, run["id"])
    second = teams.reconcile(store, run["id"])
    assert first["state"] == "ready"
    assert first["revision"] == run["revision"] == second["revision"]


def test_a_reconcile_that_fails_after_the_turn_ran_does_not_leave_the_run_running(
    store, monkeypatch
):
    sid = store.create_session()
    record, _ = setup(store)
    run = start(store, record, sid)

    def broken(*_args, **_kwargs):
        raise RuntimeError("the root chat is gone")

    monkeypatch.setattr(teams, "reconcile", broken)
    with pytest.raises(RuntimeError):
        execute(store, Recorder(), run)
    monkeypatch.undo()
    assert teams.get(store, run["id"])["state"] == "blocked"


def test_a_held_run_whose_turn_was_cut_off_by_a_restart_ends_blocked_and_can_be_cancelled(store):
    from pi.agents import team_models

    run = _after_first_step(store)
    _hold(store, run["id"], "running")
    store.mark_interrupted_turns()  # what Pi does at start for a turn left `running`
    blocked = teams.reconcile(store, run["id"])
    assert blocked["state"] == "blocked"
    cancelled = teams.finish(
        store,
        run["id"],
        team_models.Finish(expected_revision=blocked["revision"], state="cancelled"),
    )
    assert cancelled["state"] == "cancelled"
