"""Hidden-chat rule, hunt 2 (2026-11-14): vis-F1 to vis-F5.

- F1: a fork whose context policy names a message inherited from a parent now in the Trash still
  answers; the pin is left out, as `controls.history` already leaves the row out.
- F2: Inbox "Handled" drops hidden chats and hidden jobs before its 50-row limit.
- F3: Inbox impact counts follow the one rule (Trash and descendants too), not only forgetting.
- F4: restoring a kept job's chat while the chat it was kept from is in the Trash is refused.
- F5: the correction and forget previews treat a hidden chat's memory as not found.
"""

from contextlib import closing
from datetime import datetime

import httpx
import pytest

from pi.agents import inbox
from pi.context import controls as context
from pi.conversation import archive
from pi.conversation import history as session_history
from pi.conversation.store import Store
from pi.memory import edits
from pi.memory import forget as memory_forget
from pi.memory.corrections import Client
from pi.models.routing import Router
from pi.schedule import agent_jobs, job_models
from pi.turns.loop import Loop

KEY = "synthetic-correction-capability-" + "k" * 32
NOON = datetime.fromisoformat("2026-10-26T10:00:00+00:00").timestamp()


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def trash(store, ids, trashed=True):
    return archive.trash(store, archive.Trash(sessions=list(ids), trashed=trashed))


def complete_turn(store, sid):
    turn = store.start_turn(sid)
    store.append_message(sid, "assistant", "done")
    store.finish_turn(turn, "complete")
    return turn


# vis-F1


def pinned_fork(store):
    parent = store.create_session(title="parent")
    pin = store.append_message(parent, "user", "An exact pinned source")
    saved = context.load(store, parent)
    context.save(
        store,
        parent,
        context.Update(
            expected_revision=saved["revision"],
            policy=context.Policy(
                sessionInstructions="Keep exact context",
                messagePolicies={pin["id"]: "keep-exact"},
                budget=context.Budget(
                    contextWindowTokens=10000, outputReserveTokens=100, otherInputTokens=0
                ),
            ),
        ),
    )
    body = context.ReviewedFork(
        expected_revision=saved["revision"] + 1,
        expected_last_message_id=store.messages(parent)[-1]["id"],
        summary="Reviewed summary",
        request_id="visibility_hunt2_fork_001",
    )
    child = context.reviewed_fork(store, parent, body)["session_id"]
    return parent, pin, child


def test_a_pinned_fork_still_answers_while_its_parent_is_in_the_trash(store):
    parent, pin, child = pinned_fork(store)
    loop = Loop(store, Router())
    assert any(m.content == pin["content"] for m in loop._history(child))
    trash(store, [parent])
    shown = loop._history(child)  # raised missing_message before the fix
    assert all(pin["content"] not in m.content for m in shown)
    trash(store, [parent], trashed=False)
    assert any(m.content == pin["content"] for m in loop._history(child))


def test_a_pin_on_a_message_that_is_simply_gone_is_still_refused():
    policy = context.Policy(
        sessionInstructions="",
        messagePolicies={"msg_missing": "keep-exact"},
        budget=context.Budget(contextWindowTokens=10000, outputReserveTokens=1, otherInputTokens=0),
    ).model_dump()
    with pytest.raises(context.ContextError):
        context.select_history(policy, [], hidden_ids={"msg_other"})


# vis-F2 and vis-F3


def test_handled_is_not_crowded_out_by_a_trashed_chat(store):
    visible = store.create_session(title="Visible chat")
    busy = store.create_session(title="Busy chat")
    complete_turn(store, visible)
    for _ in range(55):
        complete_turn(store, busy)
    trash(store, [busy])
    handled = inbox.inbox(store)["handled"]
    assert handled, "hidden rows took every one of the 50 slots"
    assert all(item["title"] != "Busy chat" for item in handled)


def test_impact_counts_leave_out_trashed_chats_and_their_forks(store):
    parent = store.create_session(title="parent")
    fork = store.create_session(title="fork", parent_id=parent)
    complete_turn(store, parent)
    complete_turn(store, fork)
    before = inbox.inbox(store)["impact"]["today"]["answers"]
    trash(store, [parent])
    assert inbox.inbox(store)["impact"]["today"]["answers"] == before - 2


# vis-F4


def kept_job(store, session_id):
    body = job_models.AgentJobCreate.model_validate(
        dict(
            name="Kept job",
            instructions="Look around.",
            timing=dict(kind="daily", time="13:00", day=0, hours=1),
            timeZone="Asia/Jerusalem",
        )
    )
    job = agent_jobs.create(store, body, now=NOON, created_by="conker")
    with store._connect() as db:
        db.execute(
            "INSERT INTO keep_results(request_id,session_id,kind,target_id,created_at) "
            "VALUES (?,?,'ai_job',?,0)",
            (f"req_job_{job['id']}", session_id, job["id"]),
        )
        db.commit()
    return job


def job_chat(store, job_id):
    with store._connect() as db:
        row = db.execute(
            "SELECT session_id FROM agent_job_sessions WHERE job_id=?", (job_id,)
        ).fetchone()
    if row:
        return row[0]
    chat = store.create_session(title="Kept job")
    with store._connect() as db:
        db.execute("INSERT INTO agent_job_sessions(job_id,session_id) VALUES (?,?)", (job_id, chat))
        db.commit()
    return chat


def test_restoring_a_kept_jobs_chat_while_its_source_is_in_the_trash_is_refused(store):
    source = store.create_session(title="source")
    chat = job_chat(store, kept_job(store, source)["id"])
    trash(store, [chat])
    trash(store, [source])
    with pytest.raises(archive.ArchiveError) as refused:
        trash(store, [chat], trashed=False)
    assert refused.value.code == "restore_parent_first"
    assert refused.value.detail["parents"] == [source]
    trashed = {row["id"] for row in session_history.page(store, trashed="only")["results"]}
    assert {source, chat} <= trashed
    assert trash(store, [chat, source], trashed=False)["sessions"] == [chat, source]


# vis-F5


def memory_client():
    def handle(request):
        return httpx.Response(
            200,
            json={
                "id": "mem-1",
                "agent_id": "companion",
                "revision": 3,
                "text": "Words from a trashed chat",
                "source_type": "inferred",
                "confidence": "low",
            },
        )

    return Client("http://memorygate", KEY, "companion", transport=httpx.MockTransport(handle))


def keep_memory(store, chat):
    with store._connect() as db:
        db.execute(
            "INSERT INTO keep_results(request_id,session_id,kind,target_id,created_at) "
            "VALUES ('req_mem_1',?,'memory','mem-1',0)",
            (chat,),
        )
        db.commit()


def test_correction_and_forget_previews_hide_a_trashed_chats_memory(store):
    chat = store.create_session(title="private")
    keep_memory(store, chat)
    client = memory_client()
    assert edits.preview(client, "mem-1", store=store)["text"] == "Words from a trashed chat"
    assert memory_forget.preview(client, "mem-1", store=store)["revision"] == 3
    trash(store, [chat])
    with pytest.raises(edits.EditError) as refused:
        edits.preview(client, "mem-1", store=store)
    assert refused.value.status == 404
    with pytest.raises(memory_forget.ForgetError) as gone:
        memory_forget.preview(client, "mem-1", store=store)
    assert gone.value.status == 404
    body = edits.Apply(
        request_id="correction_request_0001", memory_id="mem-1", expected_revision=3, text="New"
    )
    with pytest.raises(edits.EditError) as applied:
        edits.apply(store, client, body)
    assert applied.value.status == 404
    trash(store, [chat], trashed=False)
    assert edits.preview(client, "mem-1", store=store)["revision"] == 3
