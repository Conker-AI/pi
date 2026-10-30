"""Conker's own actions need the owner (2026-10-29 review, F-20, F-21, F-25, F-27).

A private chat never remembers or schedules; a job Conker makes waits for the owner unless his
settings loosen it, and one it made on its own is held tighter in every run; Keep results stay
linked to their chat; and Conker cannot flood the Inbox with requests.
"""

from datetime import UTC, datetime

import pytest
from test_tool_turns import FakeGate, build

from pi.agents import change_requests, keep, tool_drafts
from pi.conversation import settings as session_settings
from pi.conversation.store import Store
from pi.owner import preferences as prefs
from pi.schedule import agent_jobs, jobs, proactive_budget
from pi.schedule.job_models import AgentJobCreate, StateChange, Timing
from pi.turns import access, native_tools

# 12:00 and 23:30 in the owner's default zone (Asia/Jerusalem, quiet hours 22:00-07:00).
NOON = datetime(2026, 10, 29, 10, 0, tzinfo=UTC).timestamp()
NIGHT = datetime(2026, 10, 29, 21, 30, tzinfo=UTC).timestamp()
JOB = {
    "name": "Morning check",
    "instructions": "Tell me what needs me today.",
    "repeat": "daily",
    "time": "08:00",
}


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "pi.db")
    yield value
    value.close()


def call(loop, tool_id, args, session, *, run=False, turn=None, execution=None):
    execution = execution or session_settings.execution(loop.store, session)
    return native_tools.call(
        loop, tool_id, args, session_id=session, turn_id=turn, execution=execution, run=run
    )


def privacy(store, session, memory_off):
    current = session_settings.load(store, session)
    session_settings.save(
        store,
        session,
        session_settings.Update(
            expected_revision=current["revision"],
            settings=session_settings.Settings(
                agentId="companion",
                privacy=session_settings.Privacy(memoryDisabled=memory_off, harnessDisabled=False),
            ),
        ),
    )


def ids(tools):
    return sorted(tool.id for tool in tools)


def owner_job(store, name="Daily look", created_by="owner"):
    body = AgentJobCreate(
        name=name,
        instructions="Look at my calendar.",
        timing=Timing(kind="daily", time="08:00", day=0, hours=24),
        timeZone="Asia/Jerusalem",
    )
    return agent_jobs.create(store, body, created_by=created_by)


# --- F-20: a private chat never remembers or schedules ------------------------------------


def test_a_private_chat_is_not_offered_remembering_or_scheduling(store):
    loop, _ = build(store, [], FakeGate())
    private = store.create_session(privacy={"memoryDisabled": True})
    execution = session_settings.execution(store, private)
    assert ids(native_tools.offered(loop, execution, private, None)) == [
        "conker.ask",
        "conker.draft_tool",
    ]
    open_chat = store.create_session()
    assert (
        len(
            native_tools.offered(
                loop, session_settings.execution(store, open_chat), open_chat, None
            )
        )
        == 4
    )


def test_a_private_chat_is_refused_again_when_it_calls_them(store):
    loop, _ = build(store, [], FakeGate())
    private = store.create_session(privacy={"memoryDisabled": True})
    for tool_id, args in (
        (native_tools.REMEMBER, {"text": "My PIN is 1234."}),
        (native_tools.SCHEDULE, JOB),
    ):
        result = call(loop, tool_id, args, private, run=True)
        assert not result.ok and result.result["status"] == "refused"
        assert "private" in result.result["message"]
    assert change_requests.listing(store) == [] and jobs.list_jobs(store) == []


def test_what_was_said_while_a_chat_was_private_still_counts(store):
    # Turned private, a message said, turned back: its turns read that message, so no Keep.
    loop, _ = build(store, [], FakeGate())
    session = store.create_session()
    privacy(store, session, True)
    store.append_message(session, "user", "Something private.")
    privacy(store, session, False)
    stale = {**session_settings.execution(store, session), "privacy": {"memoryDisabled": False}}
    result = call(loop, native_tools.REMEMBER, {"text": "x"}, session, run=True, execution=stale)
    assert result.result["status"] == "refused"


def test_a_job_he_approves_from_a_chat_made_private_gets_a_private_chat(store):
    loop, _ = build(store, [], FakeGate())
    session = store.create_session()
    assert call(loop, native_tools.SCHEDULE, JOB, session).result["status"] == "waiting_for_owner"
    privacy(store, session, True)
    [request] = change_requests.listing(store)
    made = change_requests.approve(store, request["id"])
    job = jobs.get_job(store, made["result"]["jobId"])
    assert job["definition"]["privacy"] == {"memoryDisabled": True, "harnessDisabled": False}
    chat = agent_jobs.session_for(store, job["id"])
    assert session_settings.load(store, chat)["settings"]["privacy"]["memoryDisabled"]
    # A replacement chat (the first one closed or forgotten) is private too.
    store.close_session(chat, "closed")
    replacement = agent_jobs._open_session(store, job["id"], job["definition"])
    assert replacement != chat
    assert session_settings.load(store, replacement)["settings"]["privacy"]["memoryDisabled"]


# --- F-21: scheduling waits for him; a job Conker made alone is held tighter ---------------


def kept_job(store, loop):
    session = store.create_session()
    result = call(loop, native_tools.SCHEDULE, JOB, session, run=True)
    assert result.result["status"] == "done"
    return jobs.get_job(store, result.result["made"]["jobId"]), session


def test_the_schedule_tool_asks_by_default():
    tool = next(t for t in native_tools.CATALOG if t.id == native_tools.SCHEDULE)
    assert tool.authorization == "owner_confirmation"
    remember = next(t for t in native_tools.CATALOG if t.id == native_tools.REMEMBER)
    assert remember.authorization == "auto"


def test_a_job_made_at_once_is_conkers_and_one_he_approves_is_his(store):
    loop, _ = build(store, [], FakeGate())
    job, session = kept_job(store, loop)
    assert job["definition"]["createdBy"] == "conker"
    assert jobs.browser_job(job).definition.createdBy == "conker"
    [row] = keep.listing(store, session)
    assert (row["kind"], row["target_id"]) == ("ai_job", job["id"])
    call(loop, native_tools.SCHEDULE, JOB, session)
    [request] = change_requests.listing(store)
    mine = change_requests.approve(store, request["id"])
    assert jobs.get_job(store, mine["result"]["jobId"])["definition"]["createdBy"] == "owner"
    assert len(keep.listing(store, session)) == 1  # his approval is no Keep result


def run_job(store, job, replies, now=NOON):
    loop, provider = build(store, replies, FakeGate())
    run = jobs.run_now(store, job["id"], "manual-run-request-" + job["id"][-8:], now=now)
    return agent_jobs.AgentJobs(store, loop).run(run["id"], now=now), loop, provider


def test_a_run_of_a_job_conker_made_never_counts_as_his_words(store):
    loop, _ = build(store, [], FakeGate())
    job, _ = kept_job(store, loop)
    receipt, _, _ = run_job(store, job, ["NOTHING"])
    assert receipt["status"] == "completed"
    assert access.owner_text(store, receipt["turnId"]) == ""
    his = owner_job(store)
    receipt, _, _ = run_job(store, his, ["NOTHING"])
    assert "Look at my calendar." in access.owner_text(store, receipt["turnId"])


def test_in_a_run_of_the_owners_job_only_the_saved_instructions_count(store):
    receipt, _, _ = run_job(store, owner_job(store), ["NOTHING"])
    with store._connect() as db:
        [sent] = db.execute(
            "SELECT m.content FROM turn_messages t JOIN messages m ON m.id=t.message_id "
            "WHERE t.turn_id=? AND t.purpose='input'",
            (receipt["turnId"],),
        ).fetchone()
    started = agent_jobs.when(NOON, "Asia/Jerusalem")
    assert "Thursday, 29 October 2026" in started and started in sent
    # The preamble and the run's date and time are Pi's, so neither counts as the owner's request.
    assert access.owner_text(store, receipt["turnId"]) == "Look at my calendar."


def test_a_job_run_cannot_make_a_job_or_draft_a_tool(store):
    his = owner_job(store)
    receipt, loop, _ = run_job(store, his, ["NOTHING"])
    session, turn = receipt["sessionId"], receipt["turnId"]
    execution = session_settings.execution(store, session, turn)
    assert ids(native_tools.offered(loop, execution, session, turn)) == [
        "conker.ask",
        "conker.remember",
    ]
    for tool_id, args in (
        (native_tools.SCHEDULE, JOB),
        (native_tools.DRAFT_TOOL, {"description": "x"}),
    ):
        result = call(loop, tool_id, args, session, run=True, turn=turn, execution=execution)
        assert result.result["status"] == "refused"
    assert len(jobs.list_jobs(store)) == 1 and change_requests.listing(store) == []


def test_runs_of_a_job_conker_made_spend_his_proactive_budget(store):
    loop, _ = build(store, [], FakeGate())
    job, _ = kept_job(store, loop)
    receipt, _, provider = run_job(store, job, ["NOTHING"], now=NIGHT)
    assert receipt == {
        "status": "failed",
        "code": "HELD_BACK",
        "reason": "Held back: it is the owner's quiet hours.",
    }
    assert provider.sent == []  # no model was asked
    # Spend the rest of today's suggestions; the next run is held back by the daily limit.
    budget = proactive_budget.status(store, now=datetime.fromtimestamp(NOON, UTC))
    proactive_budget.reserve(
        store,
        proactive_budget.Request(
            request_id="other-proactive-work-1",
            expected_revision=budget["preferenceRevision"],
            proposed=prefs.Usage(suggestions=4, researchMinutes=0, costCents=0),
        ),
        now=datetime.fromtimestamp(NOON, UTC),
    )
    receipt, _, provider = run_job(store, job, ["NOTHING"])
    assert receipt["code"] == "HELD_BACK" and "daily limit" in receipt["reason"]
    assert provider.sent == []


def test_his_own_jobs_run_on_his_schedule(store):
    his = owner_job(store)
    receipt, _, provider = run_job(store, his, ["NOTHING"], now=NIGHT)
    assert receipt["status"] == "completed" and len(provider.sent) == 1


def test_an_admitted_run_reserves_once(store):
    loop, _ = build(store, [], FakeGate())
    job, _ = kept_job(store, loop)
    receipt, _, _ = run_job(store, job, ["NOTHING"])
    run_id = jobs.runs(store, job["id"])[0]["id"]
    assert agent_jobs.admit(store, run_id, job["definition"], NOON) is None
    with store._connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM proactive_reservations WHERE request_id=?", (run_id,)
            ).fetchone()[0]
            == 1
        )
    assert receipt["status"] == "completed"


def test_conker_makes_at_most_three_jobs_a_day_on_its_own(store):
    loop, _ = build(store, [], FakeGate())
    for _ in range(agent_jobs.CONKER_PER_DAY):
        kept_job(store, loop)
    fourth = call(loop, native_tools.SCHEDULE, JOB, store.create_session(), run=True)
    assert fourth.result["status"] == "waiting_for_owner"
    [request] = change_requests.listing(store)
    assert change_requests.approve(store, request["id"])["state"] == "approved"  # he may


def test_no_request_makes_a_job_once_twenty_are_on(store):
    loop, _ = build(store, [], FakeGate())
    session = store.create_session()
    call(loop, native_tools.SCHEDULE, JOB, session)
    for index in range(agent_jobs.MAX_ENABLED):
        owner_job(store, f"Job {index}")
    refused = call(loop, native_tools.SCHEDULE, JOB, session, run=True)
    assert refused.result["status"] == "refused" and "20 AI jobs" in refused.result["message"]
    [waiting] = change_requests.listing(store)
    with pytest.raises(change_requests.ChangeRequestError) as full:
        change_requests.approve(store, waiting["id"])
    assert full.value.code == "job_limit"


def test_editing_a_job_keeps_who_made_it_and_turning_it_on_clears_the_mark(store):
    job = owner_job(store, created_by="conker")
    with store._connect() as db:
        agent_jobs.turn_off(db, [job["id"]])
    off = jobs.get_job(store, job["id"])
    assert off["definition"]["enabled"] is False
    assert off["definition"]["turnedOff"] == "chat_forgotten"
    back = jobs.set_enabled(
        store, job["id"], StateChange(expected_revision=off["revision"], enabled=True)
    )
    assert back["definition"]["turnedOff"] is None and back["definition"]["createdBy"] == "conker"


# --- F-25: who wrote a memory, and every Keep result linked to its chat --------------------


def test_a_kept_memory_is_stored_as_conkers_and_linked_to_its_chat(store):
    loop, _ = build(store, [], FakeGate())
    sent = []
    loop.remember = lambda identity, text, origin="owner": (
        sent.append((identity, origin)) or {"memoryId": "m1", "status": "remembered"}
    )
    session = store.create_session()
    done = call(
        loop, native_tools.REMEMBER, {"text": "The exam is on Thursday."}, session, run=True
    )
    request = done.result["requestId"]
    assert sent == [(request + "-conker", "conker")]
    [row] = keep.listing(store, session)
    assert (row["kind"], row["target_id"], row["undo"]) == ("memory", "m1", None)
    # A memory MemoryGate already held was not made by this chat: nothing to undo later.
    loop.remember = lambda identity, text, origin="owner": {
        "memoryId": "m0",
        "status": "already_remembered",
    }
    call(loop, native_tools.REMEMBER, {"text": "Old fact."}, session, run=True)
    assert len(keep.listing(store, session)) == 1


def test_the_statement_client_says_conker_wrote_it():
    import json

    import httpx

    from pi.memory import corrections

    seen = []

    def answer(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200, json={"agent_id": "owner", "memory_id": "m1", "status": "remembered"}
        )

    client = corrections.Client(
        "http://memory.test", "k" * 32, "owner", transport=httpx.MockTransport(answer)
    )
    client.remember("creq_" + "c" * 32 + "-conker", "Exam on Thursday.", origin="conker")
    client.remember("creq_" + "c" * 32, "Exam on Thursday.")
    assert seen == [
        {"text": "Exam on Thursday.", "origin": "conker"},
        {"text": "Exam on Thursday."},
    ]
    with pytest.raises(ValueError):
        client.remember("creq_" + "d" * 32, "x", origin="someone")


# --- F-27: Conker cannot flood the Inbox -------------------------------------------------


def test_three_open_requests_per_chat_and_twenty_in_all(store):
    loop, _ = build(store, [], FakeGate())
    session = store.create_session()
    for index in range(change_requests.PER_CHAT):
        assert call(loop, native_tools.REMEMBER, {"text": f"Fact {index}."}, session).ok
    fourth = call(loop, native_tools.REMEMBER, {"text": "One more."}, session)
    assert fourth.result["status"] == "refused" and "already wait" in fourth.result["message"]
    while len(change_requests.listing(store)) < change_requests.OPEN_LIMIT:
        change_requests.file(store, "memory", "Remember: x", "x", {"text": "x"})
    other = call(loop, native_tools.DRAFT_TOOL, {"description": "A digest"}, store.create_session())
    assert other.result["status"] == "refused"
    # His own request from Tools is never limited.
    mine = tool_drafts.submit(
        store, tool_drafts.NewDraft(description="A digest", request_id="owner-draft-request-1")
    )
    assert mine["state"] == "open"
