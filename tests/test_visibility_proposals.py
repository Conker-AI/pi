"""A trashed chat feeds no proposal, question or job note anywhere, and restoring brings them back."""

import json
import time
import types
from contextlib import closing
from datetime import UTC, datetime

import pytest

from pi.agents import asks
from pi.conversation import archive, forgetting, submissions
from pi.conversation.store import Store
from pi.models.providers import Completion
from pi.schedule import agent_jobs, proposals

DAY = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)  # 15:00 in the default zone: not quiet hours
NEXT_DAY = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)  # a new day, so a new suggestion budget


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def trash(store, sessions, trashed=True):
    archive.trash(store, archive.Trash(sessions=sessions, trashed=trashed))


def say(store, *texts, at, session=None, role="user"):
    """Messages written at a chosen time, in a new chat unless one is given."""
    session, clock = session or store.create_session(), [at]
    real = submissions.time
    submissions.time = types.SimpleNamespace(time=lambda: clock[0])
    try:
        for text in texts:
            store.append_message(session, role, text)
            clock[0] += 1
    finally:
        submissions.time = real
    return session


def clock_at(monkeypatch, module, at):
    """`module` reads `at` as the time now; the rest of the time module stays real."""
    names = {name: getattr(time, name) for name in dir(time) if not name.startswith("_")}
    monkeypatch.setattr(module, "time", types.SimpleNamespace(**{**names, "time": lambda: at}))


def trash_at(monkeypatch, store, sessions, at):
    clock_at(monkeypatch, archive, at)
    trash(store, sessions)
    monkeypatch.undo()


class Model:
    def __init__(self, *titles):
        self.titles, self.prompts = titles, []

    def __call__(self, messages):
        self.prompts.append("\n".join(message.content for message in messages))
        items = [
            {
                "title": title,
                "noticed": "You asked for this three times.",
                "suggestion": "Prepare it for you.",
                "ifApproved": "A draft appears in your Inbox; nothing is sent.",
                "evidence": ["e1", "e2", "e3"],
            }
            for title in self.titles
        ]
        return Completion(text=json.dumps({"proposals": items}), model="m", provider="local")


def listed(store, state="all"):
    return [item["title"] for item in proposals.list_proposals(store, state)["proposals"]]


def test_trashed_chat_messages_never_reach_the_proposal_model(store):
    gone = say(
        store, "secret plan one", "secret plan two", "secret plan three", at=DAY.timestamp() - 600
    )
    say(store, "book the gym", "book the gym again", "gym once more", at=DAY.timestamp() - 300)
    trash(store, [gone])
    model = Model()
    assert proposals.run_pass(store, model, now=DAY)["evidence_count"] == 3
    (prompt,) = model.prompts
    assert "gym" in prompt and "secret plan" not in prompt


def test_open_proposal_from_a_trashed_chat_is_hidden_until_restored(store):
    gone = say(store, "report", "report again", "report once more", at=DAY.timestamp() - 600)
    proposals.run_pass(store, Model("Weekly report"), now=DAY)
    (found,) = proposals.list_proposals(store)["proposals"]
    trash(store, [gone])
    assert listed(store, "open") == [] and listed(store, "all") == []
    with store._connect() as db:
        assert proposals.hidden_ids(db) == {found["id"]}
    with pytest.raises(proposals.ProposalError) as missing:
        proposals.decide(store, found["id"], proposals.Decision(decision="accept"))
    assert missing.value.status == 404
    trash(store, [gone], trashed=False)
    assert listed(store, "open") == ["Weekly report"]
    decided = proposals.decide(store, found["id"], proposals.Decision(decision="accept"))
    assert decided["state"] == "accepted" and decided["evidence"][0]["available"]


def test_declined_title_from_a_trashed_chat_is_not_shown_to_the_model_but_still_blocks(store):
    gone = say(store, "report", "report again", "report once more", at=DAY.timestamp() - 600)
    proposals.run_pass(store, Model("Weekly report"), now=DAY)
    (first,) = proposals.list_proposals(store)["proposals"]
    proposals.decide(store, first["id"], proposals.Decision(decision="never"))
    trash(store, [gone])
    say(store, "meals", "meals again", "meals once more", at=NEXT_DAY.timestamp() - 600)
    model = Model("Weekly report", "Plan meals")
    assert proposals.run_pass(store, model, now=NEXT_DAY)["created_count"] == 1
    assert "Weekly report" not in model.prompts[0]
    # The idea from the other pass is visible: its window holds no message of the trashed chat.
    assert listed(store) == ["Plan meals"]
    trash(store, [gone], trashed=False)
    assert sorted(listed(store)) == ["Plan meals", "Weekly report"]


def test_questions_from_a_trashed_chat_leave_the_inbox_and_come_back(store):
    gone, kept = store.create_session(title="gone"), store.create_session(title="kept")
    hidden = asks.file(store, gone, None, "Send the secret?", ["Yes", "No"])
    shown = asks.file(store, kept, None, "Book the gym?", None)
    trash(store, [gone])
    assert [item["id"] for item in asks.listing(store)] == [shown["id"]]
    assert [item["id"] for item in asks.needs_you(store)] == [shown["id"]]
    for call in (
        lambda: asks.get(store, hidden["id"]),
        lambda: asks.answer(store, hidden["id"], "Yes", chosen=True),
        lambda: asks.dismiss(store, hidden["id"]),
    ):
        with pytest.raises(asks.AskError) as missing:
            call()
        assert missing.value.status == 404
    trash(store, [gone], trashed=False)
    assert asks.get(store, hidden["id"])["state"] == "open"  # nothing changed while hidden
    assert {item["id"] for item in asks.listing(store)} == {hidden["id"], shown["id"]}
    assert asks.dismiss(store, hidden["id"])["state"] == "dismissed"
    with pytest.raises(asks.AskError) as again:
        asks.dismiss(store, hidden["id"])
    assert again.value.status == 409


def test_hidden_questions_do_not_fill_the_open_limit(store):
    chats = [store.create_session(title=f"c{n}") for n in range(7)]
    for number in range(asks.OPEN_LIMIT):
        asks.file(store, chats[number // asks.PER_CHAT], None, f"Question {number}?", None)
    fresh = store.create_session(title="fresh")
    with pytest.raises(ValueError):
        asks.file(store, fresh, None, "One more?", None)
    trash(store, chats)
    assert asks.file(store, fresh, None, "One more?", None)["state"] == "open"


def test_job_notes_from_a_trashed_chat_leave_the_inbox_and_come_back(store):
    gone, kept = store.create_session(title="gone"), store.create_session(title="kept")
    agent_jobs._note(store, "run_a", "job_a", gone, None, "Secret found", "", DAY.timestamp())
    agent_jobs._note(store, "run_b", "job_b", kept, None, "Gym booked", "", DAY.timestamp())
    trash(store, [gone])
    assert [note["title"] for note in agent_jobs.notes(store)] == ["Gym booked"]
    assert [item["title"] for item in agent_jobs.needs_you(store)] == ["Gym booked"]
    trash(store, [gone], trashed=False)
    assert {note["title"] for note in agent_jobs.notes(store)} == {"Secret found", "Gym booked"}


def test_a_chat_in_the_trash_before_the_first_pass_hides_none_of_its_ideas(store, monkeypatch):
    # The first pass reads from the start of history, so its window holds the trashed chat too.
    gone = say(store, "an old secret", at=DAY.timestamp() - 9000)
    trash_at(monkeypatch, store, [gone], DAY.timestamp() - 60)
    say(store, "book the gym", "book the gym again", "gym once more", at=DAY.timestamp() - 300)
    model = Model("Gym")
    assert proposals.run_pass(store, model, now=DAY)["created_count"] == 1
    assert "old secret" not in model.prompts[0]
    assert listed(store) == ["Gym"]
    (found,) = proposals.list_proposals(store)["proposals"]
    assert proposals.decide(store, found["id"], proposals.Decision(decision="accept"))


def test_a_chat_still_in_use_in_the_trash_does_not_hide_later_ideas(store, monkeypatch):
    gone = say(store, "report", "report again", "report once more", at=DAY.timestamp() - 600)
    proposals.run_pass(store, Model("Weekly report"), now=DAY)
    trash_at(monkeypatch, store, [gone], DAY.timestamp() + 60)
    # An AI job replies in its trashed chat, and the owner writes in it opened by its link.
    say(store, "job reply", at=NEXT_DAY.timestamp() - 900, session=gone, role="assistant")
    say(store, "written while trashed", at=NEXT_DAY.timestamp() - 800, session=gone)
    say(store, "meals", "meals again", "meals once more", at=NEXT_DAY.timestamp() - 600)
    model = Model("Plan meals")
    assert proposals.run_pass(store, model, now=NEXT_DAY)["created_count"] == 1
    assert "written while trashed" not in model.prompts[0]
    # The first pass read the trashed chat, so its idea stays hidden; the second did not.
    assert listed(store) == ["Plan meals"]
    trash(store, [gone], trashed=False)
    assert sorted(listed(store)) == ["Plan meals", "Weekly report"]


def test_words_no_pass_reads_do_not_hide_its_ideas_when_their_chat_is_trashed(store):
    # Visible during the pass, but only the assistant spoke in it, so the model never saw it.
    quiet = say(store, "assistant text", at=DAY.timestamp() - 900, role="assistant")
    say(store, "book the gym", "book the gym again", "gym once more", at=DAY.timestamp() - 300)
    proposals.run_pass(store, Model("Gym"), now=DAY)
    trash(store, [quiet])
    assert listed(store) == ["Gym"]


def test_a_chat_forgotten_before_the_first_pass_hides_none_of_its_ideas(tmp_path, monkeypatch):
    path = tmp_path / "pi.db"
    with closing(Store(path)) as store:
        gone = say(store, "an old secret", at=DAY.timestamp() - 9000)
    clock_at(monkeypatch, forgetting, DAY.timestamp() - 60)
    forgetting.forget(path, gone, forgetting.preview(path, gone)["confirmation"])
    monkeypatch.undo()
    with closing(Store(path)) as store:
        say(store, "book the gym", "book the gym again", "gym once more", at=DAY.timestamp() - 300)
        assert proposals.run_pass(store, Model("Gym"), now=DAY)["created_count"] == 1
        # Forgotten chats never come back, so a wrong hide here would last for ever.
        assert listed(store) == ["Gym"]
