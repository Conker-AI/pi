"""Team runs after a restart, and a role input refused before anything is saved (hunt pi-2 F2, F3)."""

import pytest
from test_team_execution import Recorder, execute, setup, start, store  # noqa: F401

from pi.agents import profiles as agents
from pi.agents import team_execution as teams
from pi.agents import team_models


def test_an_interrupted_run_can_be_cancelled_but_not_completed(store):
    class Crash(BaseException):
        pass

    def crash():
        raise Crash()

    sid = store.create_session()
    record, _ = setup(store)
    run = start(store, record, sid)
    with pytest.raises(Crash):
        execute(store, Recorder(callback=crash), run)
    store.mark_interrupted_turns()
    teams.recover_interrupted(store)
    current = teams.get(store, run["id"])
    with pytest.raises(agents.AgentError, match="Resolve"):
        teams.finish(
            store,
            run["id"],
            team_models.Finish(expected_revision=current["revision"], state="completed"),
        )
    cancelled = teams.finish(
        store,
        run["id"],
        team_models.Finish(expected_revision=current["revision"], state="cancelled"),
    )
    assert cancelled["state"] == "cancelled"


def test_too_long_input_is_refused_before_the_step_is_saved(store, monkeypatch):
    monkeypatch.setattr(teams, "MAX_INPUT_CHARS", 10)
    sid = store.create_session()
    record, _ = setup(store)
    run = start(store, record, sid)
    provider = Recorder()
    with pytest.raises(agents.AgentError, match="16000"):
        execute(store, provider, run)
    current = teams.get(store, run["id"])
    # Nothing was saved, so the run is still ready and the same request can be sent again.
    assert current["state"] == "ready" and current["steps"] == [] and provider.calls == []
