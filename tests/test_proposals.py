"""Proposal engine: budget, privacy, evidence, and remembered decisions. No network."""

import json
from contextlib import closing
from datetime import UTC, datetime

import pytest

from pi import forgetting, proactive_budget, proposals
from pi.providers import Completion
from pi.store import Store

DAY = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)  # 15:00 in the default Asia/Jerusalem zone
QUIET = datetime(2026, 9, 24, 21, 0, tzinfo=UTC)  # midnight there: inside quiet hours


class Model:
    """Records the prompt and replies with a fixed body."""

    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def __call__(self, messages):
        self.prompts.append(messages)
        body = self.reply(messages) if callable(self.reply) else self.reply
        return Completion(text=body, model="analyst", provider="local")


def proposal(title, *evidence, **extra):
    return {
        "title": title,
        "noticed": "You asked for this summary three Mondays in a row.",
        "suggestion": "Prepare it every Monday at 8:00.",
        "ifApproved": "A draft summary appears in your Inbox each Monday; nothing is sent.",
        "evidence": list(evidence),
        **extra,
    }


def reply(*items):
    return json.dumps({"proposals": list(items)})


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def say(store, *texts, at=None):
    """Owner messages written at a chosen time; history itself stays append-only."""
    import types

    from pi import submissions

    at = DAY.timestamp() - 3600 if at is None else at
    session, ids, clock = store.create_session(), [], [at]
    real = submissions.time
    submissions.time = types.SimpleNamespace(time=lambda: clock[0])
    try:
        for text in texts:
            ids.append(store.append_message(session, "user", text)["id"])
            clock[0] += 1
    finally:
        submissions.time = real
    return session, ids


def test_disabled_role_or_too_little_evidence_spends_nothing(store):
    model = Model(reply())
    say(store, "one", "two", "three")
    assert proposals.run_pass(store, model, now=DAY, role_enabled=False)["reason"] == (
        "proposals_role_disabled"
    )
    assert not model.prompts
    empty = Store(store.path.parent / "empty.db")
    with closing(empty):
        say(empty, "only one message")
        assert proposals.run_pass(empty, model, now=DAY)["reason"] == "not_enough_new_messages"
    assert not model.prompts
    assert proactive_budget.status(store, now=DAY)["reserved"]["suggestions"] == 0


def test_pass_records_cited_proposals_and_advances_the_watermark(store):
    _, ids = say(store, "Summarize my week", "Summarize my week again", "Weekly summary please")
    model = Model(
        reply(
            proposal("Weekly summary every Monday", "e1", "e2", "e3"),
            proposal("Invented evidence", "e1", "e99"),
            proposal("No evidence at all"),
            proposal("Weekly summary every Monday!", "e2"),
        )
    )
    result = proposals.run_pass(store, model, now=DAY)
    assert result["state"] == "completed" and result["created_count"] == 1
    assert result["model"] == "analyst"
    prompt = model.prompts[0][1].content
    assert "e1 [2026-09-24]: Summarize my week" in prompt and "Weekly summary please" in prompt
    listed = proposals.list_proposals(store)["proposals"]
    assert [item["title"] for item in listed] == ["Weekly summary every Monday"]
    assert [e["messageId"] for e in listed[0]["evidence"]] == ids
    assert listed[0]["evidence"][0]["excerpt"] == "Summarize my week"
    assert listed[0]["grantsExecutionAuthority"] is False
    assert proactive_budget.status(store, now=DAY)["reserved"]["suggestions"] == 3
    # The same messages are not read twice.
    assert proposals.run_pass(store, model, now=DAY)["reason"] == "not_enough_new_messages"


def private(store, memory, harness):
    from pi import session_settings

    session = store.create_session()
    session_settings.save(
        store,
        session,
        session_settings.Update(
            expected_revision=0,
            settings=session_settings.Settings(
                agentId="companion",
                privacy=session_settings.Privacy(memoryDisabled=memory, harnessDisabled=harness),
            ),
        ),
    )
    return session


def test_incognito_harness_disabled_and_forgotten_messages_are_never_read(store):
    from pi import submissions

    say(store, "visible one", "visible two", "visible three")
    real = submissions.time
    try:
        import types

        submissions.time = types.SimpleNamespace(time=lambda: DAY.timestamp() - 1000)
        store.append_message(private(store, True, False), "user", "incognito secret")
        store.append_message(private(store, False, True), "user", "no-harness secret")
    finally:
        submissions.time = real
    forgotten, _ = say(store, "forgotten secret")
    path = store.path
    store.close()
    forgetting.forget(path, forgotten, forgetting.preview(path, forgotten)["confirmation"])
    with closing(Store(path)) as reopened:
        model = Model(reply())
        proposals.run_pass(reopened, model, now=DAY)
        prompt = model.prompts[0][1].content
        assert "visible three" in prompt
        assert "secret" not in prompt


def test_budget_and_quiet_hours_deny_before_any_model_call(store):
    say(store, "a", "b", "c")
    model = Model(reply())
    denied = proposals.run_pass(store, model, now=QUIET)
    assert denied["state"] == "denied" and "quiet" in denied["reason"]
    assert not model.prompts
    with store._connect() as db:
        db.execute("DELETE FROM proposal_passes")
        db.commit()
    for index in range(2):
        proactive_budget.reserve(
            store,
            proactive_budget.Request(
                request_id=f"earlier_reservation_{index:04d}",
                expected_revision=1,
                proposed={"suggestions": 2, "researchMinutes": 0, "costCents": 0},
            ),
            now=DAY,
        )
    assert proposals.run_pass(store, model, now=DAY)["reason"] == "daily_suggestion_limit"
    assert not model.prompts


def test_unusable_model_output_keeps_the_evidence_for_next_time(store):
    say(store, "a", "b", "c")
    failed = proposals.run_pass(store, Model("I think you should relax."), now=DAY)
    assert failed["state"] == "failed" and failed["reason"] == "model_or_output_unusable"

    def broken(messages):
        raise RuntimeError("provider echoed private text")

    assert proposals.run_pass(store, broken, now=DAY)["state"] == "failed"
    assert proposals.passes(store)["watermark"] == 0.0
    assert "private" not in json.dumps(proposals.passes(store))


def test_decisions_are_final_and_declines_are_remembered(store):
    say(store, "a", "b", "c")
    proposals.run_pass(
        store,
        Model(reply(proposal("Pay rent reminder", "e1"), proposal("Gym plan", "e2"))),
        now=DAY,
    )
    rent, gym = sorted(proposals.list_proposals(store)["proposals"], key=lambda p: p["title"])[::-1]
    never = proposals.decide(store, rent["id"], proposals.Decision(decision="never"))
    assert never["state"] == "never" and never["decidedAt"]
    assert (
        proposals.decide(store, rent["id"], proposals.Decision(decision="never"))["state"]
        == "never"
    )
    with pytest.raises(proposals.ProposalError) as conflict:
        proposals.decide(store, rent["id"], proposals.Decision(decision="accept"))
    assert conflict.value.status == 409
    proposals.decide(store, gym["id"], proposals.Decision(decision="accept"))
    with store._connect() as db, pytest.raises(Exception, match=r"immutable|final"):
        db.execute("UPDATE proposals SET title='changed' WHERE id=?", (gym["id"],))
    with pytest.raises(proposals.ProposalError):
        proposals.decide(store, "prp_missing", proposals.Decision(decision="decline"))

    say(store, "d", "e", "f", at=DAY.timestamp() - 60)
    model = Model(reply(proposal("Pay  rent REMINDER", "e1"), proposal("Groceries list", "e2")))
    proposals.run_pass(store, model, now=DAY)
    assert "- Pay rent reminder" in model.prompts[0][1].content
    titles = {p["title"] for p in proposals.list_proposals(store)["proposals"]}
    assert titles == {"Groceries list"}


def test_passes_run_on_interval_and_denials_retry_sooner(store):
    assert proposals.due(store, 3600, now=DAY.timestamp())
    say(store, "a", "b", "c")
    proposals.run_pass(store, Model(reply()), now=QUIET)
    assert proposals.due(store, 3600, now=QUIET.timestamp() + 60)
    proposals.run_pass(store, Model(reply()), now=DAY)
    assert not proposals.due(store, 3600, now=DAY.timestamp() + 60)
    assert proposals.due(store, 3600, now=DAY.timestamp() + 3601)


def test_interrupted_pass_is_recorded_on_restart(store):
    with store._connect() as db:
        db.execute("INSERT INTO proposal_passes(id,started_at,state) VALUES ('pps_x',1,'running')")
        db.commit()
    proposals.recover_interrupted(store)
    assert proposals.passes(store)["passes"][0]["reason"] == "interrupted"


def test_http_surface_lists_and_decides(store):
    from fastapi.testclient import TestClient

    from pi import api

    say(store, "a", "b", "c")
    proposals.run_pass(store, Model(reply(proposal("Weekly summary", "e1"))), now=DAY)
    item = proposals.list_proposals(store)["proposals"][0]
    api.app.dependency_overrides[api.require_key] = lambda: None
    api.app.state.store = store
    try:
        client = TestClient(api.app)
        assert client.get("/proposals").json()["proposals"][0]["id"] == item["id"]
        assert client.get("/proposals?state=bogus").status_code == 422
        decided = client.post(f"/proposals/{item['id']}/decision", json={"decision": "decline"})
        assert decided.status_code == 200 and decided.json()["state"] == "declined"
        assert (
            client.post(f"/proposals/{item['id']}/decision", json={"decision": "run"}).status_code
            == 422
        )
        assert client.get("/proposals").json()["proposals"] == []
        assert client.get("/proposals/passes").json()["passes"][0]["state"] == "completed"
    finally:
        api.app.dependency_overrides.clear()


def test_small_model_quirks_are_tolerated_without_inventing_evidence(store):
    say(store, "a", "b", "c")
    quirky = {**proposal("Homework reminders", "e1", "e3"), "noticed": ["e1", "e3"]}
    proposals.run_pass(store, Model("Sure! ```json\n" + reply(quirky) + "\n```"), now=DAY)
    [item] = proposals.list_proposals(store)["proposals"]
    assert item["noticed"] == "You asked about this in 2 recent messages."
    assert len(item["evidence"]) == 2
