"""When each chat was first hidden, built once per query (pi.conversation.visibility.hidden_since).

A proposal hides when its pass read a chat that is hidden now, but not when that chat was
already hidden as the pass started (pi.schedule.proposals.HIDDEN). The "already hidden then"
test must not rebuild the hidden tree for each (proposal, message) pair: SQLite would rerun
the recursive query per row, and HIDDEN runs on every proposal list, decision and pass.
"""

import json
import time
import types
from contextlib import closing
from datetime import UTC, datetime

import pytest
from test_visibility_proposals import Model, listed, say

from pi.conversation import archive, visibility
from pi.conversation.store import Store
from pi.schedule import proposals

DAY = datetime(2026, 9, 24, 12, 0, tzinfo=UTC).timestamp()
CHATS = 200


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def trash_at(monkeypatch, store, sessions, at):
    with monkeypatch.context() as patch:
        patch.setattr(archive, "time", types.SimpleNamespace(time=lambda: at))
        archive.trash(store, archive.Trash(sessions=sessions, trashed=True))


def since(store):
    with store._connect() as db:
        return {row[0]: row[1] for row in db.execute(visibility.hidden_since())}


def plan(db, query):
    """EXPLAIN QUERY PLAN rows as (id, parent, detail)."""
    return [(row[0], row[1], row[3]) for row in db.execute("EXPLAIN QUERY PLAN " + query)]


def test_a_fork_takes_the_earliest_time_any_chat_above_it_was_hidden(store, monkeypatch):
    top = store.create_session(title="top")
    middle = store.create_session(title="middle", parent_id=top)
    bottom = store.create_session(title="bottom", parent_id=middle)
    other = store.create_session(title="other")
    trash_at(monkeypatch, store, [bottom], DAY + 50)
    trash_at(monkeypatch, store, [top], DAY + 10)
    trash_at(monkeypatch, store, [middle], DAY + 30)
    assert since(store) == {top: DAY + 10, middle: DAY + 10, bottom: DAY + 10}
    assert other not in since(store)
    archive.trash(store, archive.Trash(sessions=[top], trashed=False))
    assert since(store) == {middle: DAY + 30, bottom: DAY + 30}


def test_hidden_builds_the_hidden_tree_once_not_per_proposal(store):
    with store._connect() as db:
        rows = plan(db, proposals.HIDDEN)
    detail = {node: text for node, _, text in rows}
    parent = {node: up for node, up, _ in rows}
    # Each recursive query sits in a MATERIALIZE or CO-ROUTINE node; what holds that node is
    # computed once unless it is a CORRELATED subquery (rerun for every outer row).
    holders = [
        detail.get(parent.get(parent[node]), "")
        for node, _, text in rows
        if text == "RECURSIVE STEP"
    ]
    assert holders, rows
    assert not [text for text in holders if text.startswith("CORRELATED")], rows


def rows(db, table, *values):
    marks = ",".join("?" * len(values[0]))
    db.executemany(f"INSERT INTO {table} VALUES ({marks})", values)


def seed(store):
    """The slow case: CHATS chats with 3 messages each and a fork; every other chat went to
    the Trash before the passes started, so a pass read none of them and every one of their
    messages in its window must be checked (no early exit). Three first passes (reading from
    the start) hold 100 proposals each; a later pass read chat `late`, trashed after it.
    Written straight to the tables: the query is what is timed."""
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for number in range(CHATS):
            at, chat = DAY + number * 10, f"ses_{number}"
            db.execute("INSERT INTO sessions(id,title,created_at) VALUES (?,'',?)", (chat, at))
            db.execute(
                "INSERT INTO sessions(id,parent_id,title,created_at) VALUES (?,?,'',?)",
                (f"fork_{number}", chat, at),
            )
            for seq in range(3):
                text = json.dumps(f"note {number}.{seq}")
                rows(db, "messages", (f"msg_{number}_{seq}", chat, seq, "user", text, at + seq))
        rows(db, "sessions", ("late", None, "", "open", DAY + 7000, None, None))
        rows(db, "messages", ("msg_late", "late", 0, "user", json.dumps("late"), DAY + 7000))
        passes = [(f"pps_{n}", DAY + 6000, None, None) for n in range(3)]
        passes.append(("pps_late", DAY + 7100, DAY + 6999, DAY + 7001))
        for pass_id, started, since, until in passes:
            db.execute(
                "INSERT INTO proposal_passes(id,started_at,state,watermark_from,watermark_to) "
                "VALUES (?,?,'completed',?,?)",
                (pass_id, started, since, until or DAY + 4000),
            )
            count = 3 if pass_id == "pps_late" else 100
            for number in range(count):
                # Each cites a message of a chat that stays visible.
                cited = json.dumps([f"msg_{2 * number + 1}_0"])
                db.execute(
                    "INSERT INTO proposals(id,pass_id,fingerprint,title,noticed,suggestion,"
                    "if_approved,evidence,created_at) VALUES (?,?,?,?,'n','s','i',?,?)",
                    (
                        f"prp_{pass_id}_{number}",
                        pass_id,
                        f"{pass_id} {number}",
                        "Idea",
                        cited,
                        started,
                    ),
                )
        trashed = [(f"ses_{number}", DAY + 5000) for number in range(0, CHATS, 2)]
        rows(db, "session_trash", *trashed, ("late", DAY + 8000))
        db.commit()


def test_hidden_stays_fast_with_hundreds_of_proposals_and_trashed_chats(store):
    seed(store)
    started = time.perf_counter()
    with store._connect() as db:
        hidden = proposals.hidden_ids(db)
    elapsed = time.perf_counter() - started
    assert hidden == {f"prp_pps_late_{number}" for number in range(3)}
    assert elapsed < 2, f"HIDDEN took {elapsed:.2f} s"
    started = time.perf_counter()
    shown = proposals.list_proposals(store, "all", limit=100)["proposals"]
    assert time.perf_counter() - started < 2
    assert len(shown) == 100 and all(item["evidence"][0]["available"] for item in shown)


def test_a_fork_trashed_with_its_chat_before_the_pass_hides_nothing_after(store, monkeypatch):
    gone = say(store, "an old secret", at=DAY - 9000)
    fork = store.create_session(title="fork", parent_id=gone)
    say(store, "fork words", at=DAY - 8000, session=fork)
    trash_at(monkeypatch, store, [gone], DAY - 60)
    read = say(store, "book the gym", "book the gym again", "gym once more", at=DAY - 300)
    model = Model("Gym")
    assert proposals.run_pass(store, model, now=datetime.fromtimestamp(DAY, UTC))
    assert "fork words" not in model.prompts[0] and listed(store) == ["Gym"]
    # Trashing a fork of the chat it read hides nothing: the pass never read the fork's
    # words. Trashing the chat it read hides the idea until the chat is restored.
    later = store.create_session(title="later", parent_id=read)
    trash_at(monkeypatch, store, [later], DAY + 60)
    assert listed(store) == ["Gym"]
    trash_at(monkeypatch, store, [read], DAY + 120)
    assert listed(store) == []
    archive.trash(store, archive.Trash(sessions=[read], trashed=False))
    assert listed(store) == ["Gym"]


def seed_forgotten(store, chats, passes):
    """Many hidden chats with no messages left (each forgotten with 3 forks: forgetting keeps
    the ids for good, deletes the words) and `passes` passes of 3 proposals that read only a
    visible chat. Proposals are never deleted either, so both numbers only grow."""
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO sessions(id,title,created_at) VALUES ('seen','',0)")
        for number in range(chats):
            chat = f"gone_{number}"
            tree = [chat, *(f"{chat}_{fork}" for fork in range(3))]
            db.executemany(
                "INSERT INTO sessions(id,parent_id,title,status,created_at) "
                "VALUES (?,?,'','forgotten',0)",
                [(item, None if item == chat else chat) for item in tree],
            )
            db.execute(
                "INSERT INTO forgetting_receipts VALUES (?,?,1,'c',0,0)", (f"rcp_{number}", chat)
            )
            rows(db, "forgotten_sessions", *((item, f"rcp_{number}") for item in tree))
        for number in range(passes):
            start = 10 + number * 10
            for seq in range(10):
                text = json.dumps("x")
                rows(
                    db,
                    "messages",
                    (f"seen_{number}_{seq}", "seen", start + seq, "user", text, start + seq),
                )
            db.execute(
                "INSERT INTO proposal_passes(id,started_at,state,watermark_from,watermark_to) "
                "VALUES (?,?,'completed',?,?)",
                (f"pps_{number}", start + 10, start, start + 10),
            )
            for idea in range(3):
                db.execute(
                    "INSERT INTO proposals(id,pass_id,fingerprint,title,noticed,suggestion,"
                    "if_approved,evidence,created_at) VALUES (?,?,?,'Idea','n','s','i',?,0)",
                    (f"prp_{number}_{idea}", f"pps_{number}", f"{number} {idea}", '["seen_0_0"]'),
                )
        db.commit()


def test_a_pass_window_is_read_by_time_not_once_per_hidden_chat(store):
    # Driving the window test from the hidden chats probes messages once per (proposal,
    # hidden chat), forgotten chats included; driving it from the window reads only the
    # messages the pass could have read.
    with store._connect() as db:
        details = [text for _, _, text in plan(db, proposals.HIDDEN)]
    assert any("messages_by_time" in text for text in details), details
    assert not [text for text in details if "messages_by_session" in text], details


def test_hidden_stays_fast_with_thousands_of_forgotten_chats(store):
    seed_forgotten(store, chats=2000, passes=300)
    started = time.perf_counter()
    with store._connect() as db:
        hidden = proposals.hidden_ids(db)
    elapsed = time.perf_counter() - started
    assert hidden == set()
    assert elapsed < 1, f"HIDDEN took {elapsed:.2f} s"
