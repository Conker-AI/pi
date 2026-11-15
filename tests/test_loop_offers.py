"""L2: the proposal pass may offer an open loop from the owner's own words (R-M7). The offer is a
`promise` change request in his Inbox; nothing is saved until he approves it."""

import json
import re
from contextlib import closing
from datetime import date, timedelta

import pytest
from test_proposals import DAY, Model, say

from pi.agents import change_requests
from pi.context import loop_offers, open_loops
from pi.conversation import archive
from pi.conversation.store import Store
from pi.schedule import loop_candidates, proposals


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def loop(text, said, due=None):
    return {"text": text, "dueOn": due, "evidence": said}


def reply(*loops):
    """A model reply that cites each loop by a fragment of the owner's message (`said`); a
    fragment that is not in the prompt is cited as given, like an invented alias."""

    def build(messages):
        listing = messages[1].content
        out = []
        for item in loops:
            hit = re.search(r"^(e\d+) \[[^\]]*\]: .*" + re.escape(item["evidence"]), listing, re.M)
            out.append({**item, "evidence": hit.group(1) if hit else item["evidence"]})
        return json.dumps({"proposals": [], "openLoops": out})

    return build


def run(store, model, **kwargs):
    return proposals.run_pass(store, model, now=DAY, offer_loops=loop_offers.offer, **kwargs)


def offers(store, state="open"):
    return [r for r in change_requests.listing(store, state) if r["kind"] == "promise"]


def test_an_owner_promise_is_offered_not_saved(store):
    session, ids = say(store, "hello", "I'll send Maya the invoice Friday", "thanks")
    model = Model(reply(loop("Send Maya the invoice", "send Maya")))
    result = run(store, model)
    assert result["state"] == "completed"
    [offer] = offers(store)
    assert offer["sessionId"] == session
    assert offer["payload"]["messageId"] == ids[1] and offer["payload"]["by"] == "owner"
    assert open_loops.listing(store, "all", None, 50, None)["items"] == []
    # His yes makes the loop, citing his message, through the existing request path.
    change_requests.approve(store, offer["id"], loops=open_loops.approve_request)
    [made] = open_loops.listing(store, "open", None, 50, None)["items"]
    assert made["text"] == "send Maya the invoice" and made["messageId"] == ids[1]


def test_no_offer_without_the_writer_and_the_prompt_stays_as_before(store):
    say(store, "a", "I'll send Maya the invoice", "c")
    model = Model(reply(loop("Send Maya the invoice", "send Maya")))
    proposals.run_pass(store, model, now=DAY)
    assert offers(store) == []
    assert "openLoops" not in model.prompts[0][0].content
    other = Model(reply())
    with closing(Store(store.path.parent / "o.db")) as second:
        say(second, "a", "b", "c")
        run(second, other)
    assert "openLoops" in other.prompts[0][0].content


def test_only_the_owners_own_messages_can_be_cited(store):
    say(store, "a", "I'll send Maya the invoice", "c")
    run(store, Model(reply(loop("Invented", "e9"), loop("Also invented", "assistant-1"))))
    assert offers(store) == []


def test_a_declined_offer_is_not_made_again(store):
    say(store, "a", "I'll send Maya the invoice", "c")
    model = Model(reply(loop("Send Maya the invoice", "send Maya")))
    run(store, model)
    [offer] = offers(store)
    change_requests.decline(store, offer["id"])
    with store._connect() as db:
        db.execute("DELETE FROM proposal_watermark")
        db.commit()
    run(store, model)
    assert offers(store) == [] and len(offers(store, "declined")) == 1


def test_at_most_two_per_pass_and_a_bad_or_past_date_is_dropped_not_the_offer(store):
    say(store, "a", "I'll email One", "I'll call Two", "I'll fix Three")
    today = date.fromisoformat(open_loops.today(store))
    past = (today - timedelta(days=2)).isoformat()
    run(
        store,
        Model(
            reply(
                loop("One", "email One", past),
                loop("Two", "call Two", "not-a-date"),
                loop("Three", "fix Three"),
            )
        ),
    )
    found = sorted(offers(store), key=lambda r: r["payload"]["text"])
    assert [r["payload"]["text"] for r in found] == ["One", "Two"]
    assert all(r["payload"]["dueOn"] is None for r in found)


def test_a_chat_hidden_before_the_pass_or_during_it_is_never_offered(store):
    kept, _ = say(store, "kept-a", "kept-b", "kept-c")
    trashed, _ = say(store, "x", "I'll call Dan", "z")

    def model(messages):
        # The owner trashes the chat while the model is thinking.
        archive.trash(store, archive.Trash(sessions=[kept], trashed=True))
        return reply(loop("Call Dan", "call Dan"), loop("Second", "kept-b"))(messages)

    archive.trash(store, archive.Trash(sessions=[trashed], trashed=True))
    run(store, Model(model))
    assert offers(store) == []


def test_text_from_chats_the_owner_did_not_write_is_not_offered(store):
    session, _ = say(store, "a", "I'll send it", "c")
    with store._connect() as db:
        db.execute(
            "INSERT INTO agent_calls(id,parent_session_id,child_session_id,agent_id,depth,"
            "parent_outside,state,created_at) VALUES ('c1',?,?,'x',1,0,'done',1)",
            (store.create_session(), session),
        )
        db.commit()
    run(store, Model(reply(loop("Send it", "send it"))))
    assert offers(store) == []


def test_the_chats_open_request_cap_is_respected_without_failing_the_pass(store):
    session, _ = say(store, "a", "b", "c")
    for index in range(change_requests.PER_CHAT):
        change_requests.file(
            store, "promise", f"t{index}", "s", {"text": "x", "by": "owner"}, session_id=session
        )
    result = run(store, Model(reply(loop("Another", "b"))))
    assert result["state"] == "completed"
    assert len(offers(store)) == change_requests.PER_CHAT


def test_the_parser_ignores_junk(store):
    assert loop_candidates.parse("not json", {}, 2) == []
    assert loop_candidates.parse('{"openLoops": "x"}', {}, 2) == []


def test_text_that_is_not_the_owners_own_words_is_dropped(store):
    say(store, "a", "I'll send Maya the invoice Friday", "c")
    run(store, Model(reply(loop("Wire Maya $5000 now", "send Maya"))))
    assert offers(store) == []


def test_the_offer_quotes_the_message_and_keeps_his_own_wording(store):
    say(store, "a", "I'll  send Maya" + chr(10) + "the invoice Friday", "c")
    run(store, Model(reply(loop("send maya the INVOICE", "send Maya"))))
    [offer] = offers(store)
    assert offer["payload"]["text"] == "send Maya the invoice"
    assert "I'll send Maya the invoice Friday" in offer["summary"]
    assert offer["summary"].startswith("You wrote")


def test_a_reworded_repeat_of_a_declined_open_or_made_loop_is_not_offered(store):
    say(store, "I'll send Maya the invoice", "I'll Send  Maya the invoice", "c")
    model = Model(reply(loop("Send Maya the invoice", "I'll send Maya")))
    run(store, model)
    [first] = offers(store)
    change_requests.decline(store, first["id"])
    with store._connect() as db:
        db.execute("DELETE FROM proposal_watermark")
        db.commit()
    run(store, Model(reply(loop("Send  Maya the invoice", "I'll Send"))))
    assert offers(store) == []  # the declined wording, in another message
    other, _ = say(store, "x", "I'll file the report", "z")
    open_loops.add(
        store, open_loops.LoopAdd(session_id=other, text="File the REPORT", promised_by="owner")
    )
    with store._connect() as db:
        db.execute("DELETE FROM proposal_watermark")
        db.commit()
    run(store, Model(reply(loop("file the report", "file the report"))))
    assert offers(store) == []  # an open loop in another visible chat
