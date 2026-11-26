"""How the Morning check-in's calendar and mail reads are made and forgotten. Each read is
recorded under the run's chat before ToolGate is asked (`pi.core.check_in_reads`), so forgetting
that chat forgets both reads in ToolGate however far the run got: finished, refused before its
turn, or stopped in between. A lost answer is polled once; a stored receipt that keeps counts
only (ToolGate's mail receipt) gets one fresh read under a new id, never "no mail".
"""

from __future__ import annotations

from contextlib import closing

import pytest
from test_calendar_checkin import NOON, Calendar, Published, run
from test_mail_checkin import Mailbox, block, mail, utc
from test_tool_turns import FakeGate, build

from pi.actions import tool_forgets
from pi.context import background_reads, calendar_events, loop_briefing, recent_mail
from pi.conversation import archive, forgetting, submissions
from pi.conversation.store import Store
from pi.core import check_in_reads
from pi.core.turn_failures import TurnFailed
from pi.schedule import agent_jobs, jobs, rhythm
from pi.schedule.job_models import AgentJobCreate, Timing


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def both():
    """Pi's ToolGate client with the owner's calendar and mail both set up."""
    return Mailbox(calendar=Calendar())


def reads(run_id):
    return {check_in_reads.calendar_action(run_id), check_in_reads.mail_action(run_id)}


def recorded(store, action_id=None):
    with store._connect() as db:
        rows = db.execute(
            "SELECT action_id, session_id, run_id, tool_id, agent_id FROM check_in_reads "
            "WHERE ? IS NULL OR action_id=? ORDER BY tool_id",
            (action_id, action_id),
        ).fetchall()
    return [tuple(row) for row in rows]


class Forgets:
    """ToolGate's `POST /v2/agent/forget`: every action id it is sent is blanked there."""

    execution_key = "pi-execution"

    def __init__(self):
        self.sent: list[tuple] = []

    def forget_records(self, request_ids, action_ids, agent):
        self.sent.append((list(request_ids), list(action_ids), agent))
        empty = {"forgotten": [], "already": [], "refused": [], "unknown": []}
        return {
            "requests": {**empty, "unknown": list(request_ids)},
            "actions": {**empty, "forgotten": list(action_ids)},
        }


def forget(path, session):
    """Forget the chat, then let Pi send its queue to ToolGate; (preview, what ToolGate got,
    the queue's rows afterwards)."""
    plan = forgetting.preview(path, session)
    forgetting.forget(path, session, plan["confirmation"])
    gate = Forgets()
    with closing(Store(path)) as store:
        tool_forgets.drain(store, gate, now=NOON + 3600)
        with store._connect() as db:
            rows = {row[0]: row[1] for row in db.execute("SELECT id, state FROM toolgate_forgets")}
    sent = {identity for _, actions, _ in gate.sent for identity in actions}
    return plan, sent, rows


def test_each_read_is_recorded_under_the_runs_chat_before_it_is_sent(store):
    client = both()
    seen = []
    client.before = lambda action_id: seen.append(recorded(store, action_id))
    _, _, session, run_id = run(store, client, "check-in-forget-0001")
    calendar, mail = check_in_reads.calendar_action(run_id), check_in_reads.mail_action(run_id)
    assert seen == [
        [(calendar, session, run_id, calendar_events.TOOL, "companion")],
        [(mail, session, run_id, recent_mail.TOOL, "companion")],
    ]
    assert len(client.calendar.calls) == len(client.calls) == 1


def test_forgetting_the_check_in_chat_removes_both_reads(tmp_path):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        text, _, session, run_id = run(store, both(), "check-in-forget-0002")
        assert "<calendar-events>" in text and "<recent-mail>" in text
        mine = AgentJobCreate(
            name="Mine",
            instructions="Look at my week.",
            timing=Timing(kind="daily", time="09:00", day=0, hours=24),
            timeZone="Asia/Jerusalem",
        )
        job = agent_jobs.create(store, mine, now=NOON)
        _, _, elsewhere, _ = run(store, both(), "check-in-forget-0003", job)
        with store._connect() as db:
            assert not reads(run_id) & {row[1] for row in tool_forgets.records(db, [elsewhere])}
    plan, sent, rows = forget(path, session)
    assert reads(run_id) <= set(plan["tool_record_ids"]["action"])
    assert reads(run_id) <= sent
    assert all(rows[identity] == "forgotten" for identity in reads(run_id))


def test_a_run_stopped_between_the_reads_and_its_turn_still_forgets_both(tmp_path, monkeypatch):
    # Pi stopped after the reads and before the turn was recorded: no submission, no receipt,
    # so nothing about the run itself names the chat the reads were made for.
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        client = both()
        job = rhythm.seed(store, now=NOON)
        loop, _ = build(store, ["NOTHING"], FakeGate())

        def stopped(*_args, **_kwargs):
            raise SystemExit("Pi stopped")

        monkeypatch.setattr(loop, "run_turn", stopped)
        executor = agent_jobs.AgentJobs(
            store, loop, Published(client), briefing=loop_briefing.for_job
        )
        started = jobs.run_now(store, job["id"], "check-in-forget-0004", now=NOON)
        with pytest.raises(SystemExit):
            executor.run(started["id"], now=NOON)
        assert len(client.calendar.calls) == len(client.calls) == 1
        session = agent_jobs.session_for(store, job["id"])
        with store._connect() as db:
            assert not db.execute("SELECT 1 FROM turn_submissions").fetchone()
            assert jobs.get_run(store, started["id"]).get("receipt") is None
    plan, sent, rows = forget(path, session)
    assert reads(started["id"]) <= set(plan["tool_record_ids"]["action"])
    assert reads(started["id"]) <= sent
    assert all(rows[identity] == "forgotten" for identity in reads(started["id"]))


def test_a_check_in_whose_turn_never_started_is_found_by_its_receipt(store, monkeypatch):
    client = both()
    job = rhythm.seed(store, now=NOON)
    loop, _ = build(store, ["NOTHING"], FakeGate())

    def refused(*_args, **_kwargs):
        raise TurnFailed("Send at most 16000 characters per message; split longer text.")

    monkeypatch.setattr(loop, "run_turn", refused)
    executor = agent_jobs.AgentJobs(store, loop, Published(client), briefing=loop_briefing.for_job)
    started = jobs.run_now(store, job["id"], "check-in-forget-0005", now=NOON)

    def invoke(*_args, action_id, **_kwargs):
        return executor.run(action_id, now=NOON)

    assert jobs.dispatch_claim(store, started, invoke) == "failed" and len(client.calls) == 1
    receipt = jobs.get_run(store, started["id"])["receipt"]
    other = store.create_session()
    with store._connect() as db:
        assert not db.execute("SELECT 1 FROM turn_submissions").fetchone()
        found = {row[1] for row in tool_forgets.records(db, [receipt["sessionId"]])}
        assert reads(started["id"]) <= found
        assert not reads(started["id"]) & {row[1] for row in tool_forgets.records(db, [other])}


def test_without_a_run_and_a_chat_nothing_is_read(store):
    client = both()
    chat = store.create_session()
    for text in (calendar_events.check_in_text, recent_mail.check_in_text):
        assert text(store, client=client, run_id="run-1", started_at=NOON) == ""
        assert text(store, client=client, session_id=chat, started_at=NOON) == ""
    for missing in (("run-1", ""), ("", chat)):
        args = {"hours": 24}
        answer = background_reads.answer(
            store, client, recent_mail.TOOL, args, action_id="x", agent_id="companion", chat=missing
        )
        assert answer is None
    assert client.calls == client.calendar.calls == [] and recorded(store) == []


def test_a_read_repeated_in_a_new_chat_belongs_to_both_chats(store):
    # A repeated dispatch after the run's chat was replaced reads the same action again there.
    client, first, second = both(), store.create_session(), store.create_session()
    for chat in (first, second):
        recent_mail.check_in_text(
            store, client=client, run_id="run-2", session_id=chat, started_at=NOON
        )
    read = check_in_reads.mail_action("run-2")
    assert {row[1] for row in recorded(store, read)} == {first, second}
    with store._connect() as db:
        for chat in (first, second):
            assert ("action", read, "companion") in tool_forgets.records(db, [chat])


def test_a_timeout_then_poll_gets_counts_only_and_rereads_once(tmp_path):
    with closing(Store(tmp_path / "live.db")) as live:
        expected, _, _, _ = run(live, Mailbox(), "check-in-reads-0001")
    with closing(Store(tmp_path / "pi.db")) as store:
        mailbox = Mailbox(timeouts=1)  # ToolGate read the mail; its answer never reached Pi
        text, outcome, session, run_id = run(store, mailbox, "check-in-reads-0001")
        read = check_in_reads.mail_action(run_id)
        first, fresh = [call[2] for call in mailbox.calls]
        assert first == read and mailbox.polls == [read] and fresh != read
        assert outcome["status"] == "completed" and text == expected  # all three messages
        assert len(block(text)[1]) == 3
        with store._connect() as db:
            found = {row[1] for row in tool_forgets.records(db, [session])}
        assert {read, fresh} <= found and {read, fresh} == {row[0] for row in recorded(store)}


def test_when_the_fresh_read_is_lost_too_the_check_in_runs_without_mail(tmp_path):
    with closing(Store(tmp_path / "plain.db")) as plain:
        expected, _, _, _ = run(plain, None, "check-in-reads-0002")
    with closing(Store(tmp_path / "pi.db")) as store:
        mailbox = Mailbox(timeouts=2)
        text, outcome, _, _ = run(store, mailbox, "check-in-reads-0002")
        assert len(mailbox.calls) == len(mailbox.polls) == 2  # one fresh read, never a third
    assert outcome["status"] == "completed"
    assert text == expected and "recent-mail" not in text and recent_mail.UNREAD not in text


def test_a_refused_fresh_read_runs_without_mail(tmp_path):
    with closing(Store(tmp_path / "plain.db")) as plain:
        expected, _, _, _ = run(plain, None, "check-in-reads-0003")
    with closing(Store(tmp_path / "pi.db")) as store:
        mailbox = Mailbox(timeouts=1)

        def refuse_again(action_id):
            if mailbox.calls:  # the fresh read: over ToolGate's limit of reads a minute
                mailbox.refuse = ("RATE_LIMITED", "Too many reads; try again later.")

        mailbox.before = refuse_again
        text, outcome, _, _ = run(store, mailbox, "check-in-reads-0003")
    assert outcome["status"] == "completed" and text == expected


def test_a_replay_after_new_mail_fails_as_a_conflict_and_runs_nothing_twice(store):
    mailbox = Mailbox()
    text, _, session, run_id = run(store, mailbox, "check-in-reads-0004")
    mailbox.messages = [mail(60, utc(9, 55), "Arrived since", address="late@example.com")]
    loop, _ = build(store, ["NOTHING"], FakeGate())
    executor = agent_jobs.AgentJobs(store, loop, Published(mailbox), briefing=loop_briefing.for_job)
    with pytest.raises(submissions.SubmissionError):
        executor.run(run_id, now=NOON + 600)
    users = [m["content"] for m in store.messages(session) if m["role"] == "user"]
    assert users == [text] and "Arrived since" not in text


def test_a_calendar_read_from_before_the_record_is_still_found_by_its_run(store):
    # Reads made by a Pi without `check_in_reads` (calendar only) are found from the run.
    _, _, session, run_id = run(store, both(), "check-in-reads-0005")
    with store._connect() as db:
        db.execute("DELETE FROM check_in_reads")
        found = {row[1] for row in tool_forgets.records(db, [session])}
    assert check_in_reads.calendar_action(run_id) in found
    assert check_in_reads.mail_action(run_id) not in found  # mail never ran without the record


def test_a_stored_receipt_is_never_taken_for_the_read(store):
    # Both reads lost on the way back: the polls give counts and UIDs only, never "no mail".
    mailbox, chat = Mailbox(timeouts=2), store.create_session()
    answer = background_reads.answer(
        store,
        mailbox,
        recent_mail.TOOL,
        {"hours": 24},
        action_id="pi-mail-0",
        agent_id="companion",
        chat=("run-3", chat),
    )
    assert answer is None and len(mailbox.polls) == 2


def dispatched(store, client, request_id, *, loop=None, now=NOON):
    """One check-in run dispatched and finished as the worker does; (its chat, its run id)."""
    rhythm.seed(store, now=NOON)  # once; later calls make nothing
    job = jobs.get_job(store, check_in_reads.MORNING_CHECK_IN)
    loop = loop or build(store, ["NOTHING"], FakeGate())[0]
    executor = agent_jobs.AgentJobs(store, loop, Published(client), briefing=loop_briefing.for_job)
    started = jobs.run_now(store, job["id"], request_id, now=now)

    def invoke(*_args, action_id, **_kwargs):
        return executor.run(action_id, now=now)

    assert jobs.dispatch_claim(store, started, invoke) == "completed"
    return agent_jobs.session_for(store, job["id"]), started["id"]


def test_forgetting_only_the_child_of_a_forked_check_in_chat_finds_every_read(tmp_path):
    # The reused chat outgrew the fork threshold: today's turn, and so its mail, is in a child.
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        parent, earlier = dispatched(store, both(), "check-in-fork-run-0001")
        loop, _ = build(store, ["Earlier check-ins.", "NOTHING"], FakeGate())
        loop.fork_threshold_chars = 200
        client = Mailbox(calendar=Calendar(), timeouts=1)  # and a fresh read under a new id
        _, today_run = dispatched(store, client, "check-in-fork-run-0002", loop=loop, now=NOON + 60)
        with store._connect() as db:
            (child,) = [
                row[0] for row in db.execute("SELECT id FROM sessions WHERE parent_id=?", (parent,))
            ]
        assert any("<recent-mail>" in m["content"] for m in store.messages(child))
        today = {row[0] for row in recorded(store) if row[2] == today_run}
        assert len(today) == 3 and {row[1] for row in recorded(store) if row[0] in today} == {
            parent
        }
    plan, sent, rows = forget(path, child)
    assert today <= set(plan["tool_record_ids"]["action"]) and today <= sent
    assert all(rows[identity] == "forgotten" for identity in today)
    assert not reads(earlier) & sent  # the parent's own reads stay until the parent is forgotten


def test_a_check_in_chat_in_the_trash_gets_no_new_mail(store):
    trashed, _ = dispatched(store, both(), "check-in-trash-run-0001")
    archive.trash(store, archive.Trash(sessions=[trashed], trashed=True))
    before = len(store.messages(trashed))
    chat, run_id = dispatched(store, both(), "check-in-trash-run-0002", now=NOON + 60)
    assert chat != trashed and len(store.messages(trashed)) == before
    assert any("<recent-mail>" in m["content"] for m in store.messages(chat))
    assert {row[1] for row in recorded(store) if row[2] == run_id} == {chat}
