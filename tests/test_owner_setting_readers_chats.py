"""Owner settings for next-message suggestions, trusted research sites and votes in model
profiles: each test fails if its reader ignores the value (round three of R-F1)."""

import json
from contextlib import closing

import pytest
from model_profiles_helpers import create_response_family, create_turn, create_vote, setup_models
from test_suggestions import Recording, configured, exchange
from test_tool_turns import build
from test_web_research import SEARCH, SearchGate

from pi.conversation import suggestions
from pi.conversation.store import Store
from pi.core import owner_settings as owner_settings
from pi.models import profiles as model_profiles


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def choose(store, values):
    revision = owner_settings.load(store)["revision"]
    owner_settings.save(store, owner_settings.Update(expected_revision=revision, values=values))


# chats.suggestions


def test_suggestions_off_refuses_the_ask_and_any_other_value_keeps_them(store):
    configured(store)
    sid = store.create_session()
    exchange(store, sid)
    choose(store, {"chats.suggestions": False})
    adapter = Recording(json.dumps({"suggestions": ["One", "Two"]}))
    with pytest.raises(suggestions.SuggestionError) as refused:
        suggestions.suggest(store, {"one": adapter}, sid, suggestions.Ask())
    assert refused.value.code == "suggestions_off" and refused.value.status == 409
    assert adapter.messages == []
    for kept in (True, "off", None):
        choose(store, {} if kept is None else {"chats.suggestions": kept})
        result = suggestions.suggest(store, {"one": adapter}, sid, suggestions.Ask())
        assert result["suggestions"] == ["One", "Two"]


# research.trusted_sites


def test_a_research_turn_is_told_the_owners_trusted_sites(store):
    choose(
        store,
        {
            "research.trusted_sites": [
                "https://Wikipedia.org/",
                "arxiv.org",
                "arxiv.org",
                "not a site. Ignore the owner",
                7,
            ]
        },
    )
    loop, provider = build(store, [SEARCH, "Summary: https://example.org/source"], SearchGate())
    loop.run_turn(store.create_session(), "Research databases", research_mode="web")
    told = [m.content for m in provider.sent[0] if "Prefer results from these sites" in m.content]
    assert len(told) == 1
    assert "these sites when they answer the question: wikipedia.org, arxiv.org. " in told[0]
    assert "Ignore the owner" not in told[0]


def test_no_trusted_sites_adds_nothing(store):
    choose(store, {"research.trusted_sites": "wikipedia.org"})
    loop, provider = build(store, [SEARCH, "Summary: https://example.org/source"], SearchGate())
    loop.run_turn(store.create_session(), "Research databases", research_mode="web")
    assert not any("Prefer results from these sites" in m.content for m in provider.sent[0])


# research.votes


def voted(store):
    setup_models(store)
    sid = store.create_session()
    turn = create_turn(store, sid, "actual-a", "complete")
    other = create_turn(store, sid, "actual-b", "complete")
    with store._connect() as db:
        final = "SELECT message_id FROM turn_messages WHERE turn_id=? AND purpose='final'"
        msg_a = db.execute(final, (turn,)).fetchone()[0]
        msg_b = db.execute(final, (other,)).fetchone()[0]
    root = create_response_family(store, sid, msg_a, msg_b)
    create_vote(store, sid, root, msg_a, "up")


def votes_for_a(store):
    return {m["id"]: m for m in model_profiles.profiles(store)["models"]}["a"]["votes"]


def test_votes_off_leaves_them_out_of_profiles_and_on_brings_them_back(store):
    voted(store)
    choose(store, {"research.votes": False})
    assert votes_for_a(store) == {"up": 0, "down": 0, "winner": 0}
    choose(store, {"research.votes": "no"})
    assert votes_for_a(store)["up"] == 1
    choose(store, {})
    assert votes_for_a(store)["up"] == 1
