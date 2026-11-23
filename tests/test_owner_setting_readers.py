"""Owner settings that change behaviour: each test fails if its reader ignores the value."""

from contextlib import closing

import pytest
from test_agent_jobs import Loop, ai_job, claimed_run
from test_agent_jobs import reply as job_reply
from test_proposals import DAY, Model, proposal, reply, say

from pi.agents import asks
from pi.conversation import output_settings
from pi.conversation.store import Store
from pi.core import owner_settings as owner_settings
from pi.schedule import agent_job_text, agent_jobs, job_notes, proposals


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def choose(store, values):
    revision = owner_settings.load(store)["revision"]
    owner_settings.save(store, owner_settings.Update(expected_revision=revision, values=values))


# files.answer_format, files.answer_length, you.answer_language


def test_output_left_on_auto_follows_the_owner(store):
    session = store.create_session()
    with store._connect() as db:
        assert output_settings.snapshot(db, session) is None
    choose(
        store,
        {
            "files.answer_format": "table",
            "files.answer_length": "short",
            "you.answer_language": "he",
        },
    )
    with store._connect() as db:
        snapshot = output_settings.snapshot(db, session)
    assert snapshot == {"format": "table", "length": "short", "language": "he"}
    [message] = output_settings.messages({"output": snapshot})
    assert "Answer format: a table." in message.content
    assert "Answer length: short" in message.content and "Answer language: he" in message.content
    # What the chat saved wins over the owner's choice.
    output_settings.save(
        store,
        session,
        output_settings.Update(
            expected_revision=0, output=output_settings.Output(format="list", language="ru")
        ),
    )
    with store._connect() as db:
        snapshot = output_settings.snapshot(db, session)
    assert snapshot == {"format": "list", "length": "short", "language": "ru"}


@pytest.mark.parametrize(
    "values",
    [
        {"files.answer_format": "json"},
        {"files.answer_format": "poem", "files.answer_length": 3},
        {"you.answer_language": "same"},
        {"you.answer_language": "not a language"},
    ],
)
def test_auto_or_invalid_owner_output_adds_nothing(store, values):
    session = store.create_session()
    choose(store, values)
    with store._connect() as db:
        assert output_settings.snapshot(db, session) is None


# inbox.asks_per_chat, inbox.asks_total


def test_questions_per_chat_follow_the_owner(store):
    session = store.create_session()
    choose(store, {"inbox.asks_per_chat": 1})
    asks.file(store, session, None, "First?", None)
    with pytest.raises(ValueError, match="already wait"):
        asks.file(store, session, None, "Second?", None)
    # Out of the registry's range: the default of 3 applies again.
    choose(store, {"inbox.asks_per_chat": 50})
    asks.file(store, session, None, "Second?", None)
    asks.file(store, session, None, "Third?", None)
    with pytest.raises(ValueError, match="already wait"):
        asks.file(store, session, None, "Fourth?", None)


def test_questions_in_all_follow_the_owner(store):
    choose(store, {"inbox.asks_total": 5})
    for index in range(5):
        asks.file(store, store.create_session(), None, f"Question {index}?", None)
    with pytest.raises(ValueError, match="many questions"):
        asks.file(store, store.create_session(), None, "One more?", None)
    choose(store, {"inbox.asks_total": 6})
    asks.file(store, store.create_session(), None, "One more?", None)


# proactivity.skip_topics


def test_ideas_about_a_skipped_topic_are_never_proposed(store):
    say(store, "Book my checkup", "Track my steps", "Summarize my week")
    choose(store, {"proactivity.skip_topics": ["  Health ", 7, ""]})
    model = Model(
        reply(
            proposal("Weekly HEALTH check", "e1", "e2"),
            proposal("Weekly summary", "e3", suggestion="Prepare it."),
        )
    )
    result = proposals.run_pass(store, model, now=DAY)
    assert result["created_count"] == 1
    assert [item["title"] for item in proposals.list_proposals(store)["proposals"]] == [
        "Weekly summary"
    ]
    assert "Topics to skip:\n- Health\n\nRecent owner messages:" in model.prompts[0][1].content


def test_without_skipped_topics_the_prompt_is_unchanged(store):
    say(store, "Book my checkup", "Track my steps", "Summarize my week")
    model = Model(reply(proposal("Weekly health check", "e1", "e2")))
    assert proposals.run_pass(store, model, now=DAY)["created_count"] == 1
    assert "Topics to skip" not in model.prompts[0][1].content


# proactivity.jobs.results


def run_job(store, text):
    job = agent_jobs.create(store, ai_job())
    run = claimed_run(store, job)
    loop = Loop(job_reply(text, agent_jobs.session_for(store, job["id"])))
    return agent_jobs.AgentJobs(store, loop).run(run["id"])


def test_job_results_never_leave_no_note(store):
    choose(store, {"proactivity.jobs.results": "never"})
    assert run_job(store, "Physics exam on Thursday.")["noted"] is False
    assert job_notes.notes(store) == []


def test_job_results_always_note_a_quiet_run(store):
    choose(store, {"proactivity.jobs.results": "always"})
    assert run_job(store, "NOTHING")["noted"] is True
    [note] = job_notes.notes(store)
    assert note["title"] == agent_job_text.QUIET_TITLE


def test_an_unknown_job_results_value_keeps_the_default(store):
    choose(store, {"proactivity.jobs.results": "sometimes"})
    assert run_job(store, "NOTHING")["noted"] is False
    assert run_job(store, "Physics exam on Thursday.")["noted"] is True
