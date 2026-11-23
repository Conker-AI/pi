"""Reconcile's invariants: only a completed turn releases a held run (ADR-0006), a repeated or
concurrent reconcile changes nothing, and a turn whose run cannot be read never leaves it running."""

import threading

import pytest

from pi.agents import team_execution as teams
from pi.schedule import tasks
from tests.team_execution_helpers import Recorder, execute, setup, start, store  # noqa: F401
from tests.test_team_reconcile import _after_first_step, _hold

# Every turn status other than `complete`; `cancelled` is a declined or expired approval.
NOT_COMPLETE = [
    "awaiting_approval",
    "awaiting_budget",
    "acted_no_reply",
    "action_in_progress",
    "outcome_unknown",
    "failed",
    "cancelled",
    "interrupted",
    "running",
    "preparing",
]


@pytest.mark.parametrize("turn_status", NOT_COMPLETE)
def test_only_a_completed_turn_releases_a_held_run(store, turn_status):
    run = _after_first_step(store)
    _hold(store, run["id"], turn_status)
    first = teams.reconcile(store, run["id"])
    assert first["state"] != "ready"
    second = teams.reconcile(store, run["id"])
    assert (second["state"], second["revision"]) == (first["state"], first["revision"])


def test_reconciles_at_once_on_a_resumed_held_run_change_nothing(store):
    run = _after_first_step(store)
    _hold(store, run["id"], "running")
    before = teams.get(store, run["id"])
    results, errors = [], []

    def poll():
        try:
            results.extend(teams.reconcile(store, run["id"]) for _ in range(5))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=poll) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and len(results) == 20
    assert {(r["state"], r["revision"]) for r in results} == {("held", before["revision"])}


def test_a_root_chat_closed_during_the_role_turn_leaves_the_run_blocked(store):
    sid = store.create_session()
    record, _ = setup(store)
    run = start(store, record, sid)

    def close_root():
        store.close_session(run["rootSessionId"], "closed")

    with pytest.raises(tasks.TaskError, match="open conversation"):
        execute(store, Recorder(callback=close_root), run)
    with store._connect() as db:
        run_state = db.execute("SELECT state FROM team_runs WHERE id=?", (run["id"],)).fetchone()
        steps = db.execute("SELECT state FROM team_steps WHERE run_id=?", (run["id"],)).fetchall()
    assert run_state["state"] == "blocked" and [s["state"] for s in steps] == ["blocked"]
