"""The owner's daily spending cap applies to every model call a team role makes, and a cap that
cannot be checked stops the call (fail closed). Real Loop, real team metering, real ledger."""

from contextlib import closing

import pytest
from test_spending_cap import set_cap
from test_team_execution import execute, setup, start

from pi.agents import team_execution as teams
from pi.agents import team_models
from pi.agents.profiles import AgentError
from pi.conversation.store import Store
from pi.models import spending_cap
from pi.models.providers import Completion
from pi.turns.failures import TurnFailed


class Paid:
    """A paid adapter under the id the team's model route uses; it answers with reported tokens."""

    name, timeout, allow_paid, api_key = "recorder", 30.0, True, "synthetic-key"

    def __init__(self, tokens=2):
        self.calls, self.tokens = [], tokens

    def complete_bounded(self, messages, *, model, timeout):
        self.calls.append(model)
        return Completion(
            "role result",
            model,
            self.name,
            self.tokens,
            self.tokens,
            cost_usd=None,
            citations=[],
        )


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "team.db")) as value:
        yield value


def capped(store, paid):
    return spending_cap.Capped(paid, store, lambda model: True)


def run_team(store, paid):
    sid = store.create_session()
    record, _ = setup(store, tokens=400_000, cost=400_000)
    run = start(store, record, sid)
    return run, capped(store, paid), record


def another_run(store, record):
    body = team_models.Start(
        request_id="team_run_request_002",
        expected_revision=1,
        source_session_id=store.create_session(),
        text="Another task",
        budgetMode="observed-stop",
        acknowledgeCurrentCallMayOvershoot=True,
    )
    return teams.start(store, record["id"], body)


def step(store, run, provider, **changes):
    return execute(store, provider, run, **changes)


def test_a_role_call_is_counted_in_the_daily_ledger(store):
    paid = Paid(tokens=7)
    run, provider, _record = run_team(store, paid)
    assert step(store, run, provider)["state"] == "ready"
    meter = spending_cap.status(store)
    assert paid.calls == ["frozen-route"]
    assert meter["used"]["calls"] == 1 and meter["used"]["tokens"] == 14


def test_a_reached_cap_stops_the_role_before_any_paid_call(store):
    set_cap(store, tokens=10)  # the role's input alone is more than what is left
    paid = Paid()
    run, provider, _record = run_team(store, paid)
    with pytest.raises(TurnFailed):
        step(store, run, provider)
    assert paid.calls == []
    assert spending_cap.status(store)["used"]["calls"] == 0
    assert teams.get(store, run["id"])["state"] != "completed"


def test_the_cap_reached_by_the_first_role_stops_the_second(store):
    set_cap(store, tokens=5_000)
    paid = Paid(tokens=3_000)  # 6,000 tokens reported: past the cap after one call
    run, provider, _record = run_team(store, paid)
    first = step(store, run, provider)
    assert len(paid.calls) == 1 and spending_cap.status(store)["reached"] == ["tokens"]
    with pytest.raises(TurnFailed):
        step(store, first, provider, second=True)
    assert len(paid.calls) == 1


def test_a_cap_the_ledger_cannot_admit_against_fails_closed_at_the_call(store, monkeypatch):
    paid = Paid()
    run, provider, _record = run_team(store, paid)

    def unreadable(*args, **kwargs):
        raise OSError("ledger unreadable")

    monkeypatch.setattr(spending_cap, "_admit", unreadable)
    with pytest.raises(TurnFailed):
        step(store, run, provider)
    assert paid.calls == []


def test_a_cap_that_cannot_be_read_refuses_to_start_or_step_a_run(store, monkeypatch):
    paid = Paid()
    run, provider, record = run_team(store, paid)

    def unreadable(db):
        raise OSError("preferences unreadable")

    monkeypatch.setattr(spending_cap, "_preferences", unreadable)
    with pytest.raises(AgentError) as stopped:
        step(store, run, provider)
    assert stopped.value.detail["code"] == "spending_cap_unavailable" and paid.calls == []
    with pytest.raises(AgentError) as refused:
        another_run(store, record)
    assert refused.value.detail["code"] == "spending_cap_unavailable"


def test_a_run_does_not_start_or_continue_while_the_cap_is_off(store):
    paid = Paid()
    run, provider, record = run_team(store, paid)
    first = step(store, run, provider)
    set_cap(store, enabled=False)
    with pytest.raises(AgentError) as stopped:
        step(store, first, provider, second=True)
    assert stopped.value.detail["code"] == "spending_cap_off" and len(paid.calls) == 1
    with pytest.raises(AgentError) as refused:
        another_run(store, record)
    assert refused.value.detail["code"] == "spending_cap_off"
    set_cap(store, enabled=True)
    assert step(store, first, provider, second=True)["state"] == "ready"


def test_a_replayed_start_reads_back_the_run_even_with_the_cap_off(store):
    sid = store.create_session()
    record, _ = setup(store, tokens=400_000, cost=400_000)
    run = start(store, record, sid)
    set_cap(store, enabled=False)
    assert start(store, record, sid)["id"] == run["id"]
