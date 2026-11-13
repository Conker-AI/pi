"""An agent Conker kept from a hidden chat is unknown to the model's own agent tools.

`conker.run_agent` (pi.agents.agent_calls._target) and `conker.create_team`
(pi.agents.agent_requests._agent) find an agent by id or name. One Conker kept from a chat that
is now in the Trash or forgotten carries that chat's words (its instructions), so it is refused
exactly like an agent that does not exist; restoring the chat makes it reachable again.
"""

from contextlib import closing

import pytest
from test_visibility_kept_lists import make_agent

from pi.agents import agent_calls, agent_requests
from pi.conversation import archive
from pi.conversation.store import Store


@pytest.fixture
def store(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as value:
        yield value


def lookups(store, wanted):
    with store._connect() as db:
        return [agent_calls._target(db, wanted)["id"], agent_requests._agent(db, wanted)["id"]]


def refused(store, wanted):
    messages = []
    with store._connect() as db:
        for find, refusal in (
            (agent_calls._target, agent_calls.Refused),
            (agent_requests._agent, agent_requests.Refused),
        ):
            with pytest.raises(refusal) as caught:
                find(db, wanted)
            messages.append(str(caught.value))
    return messages


def test_a_kept_agent_of_a_trashed_chat_is_unknown_by_name_and_id_until_restored(store):
    hidden = store.create_session(title="hidden")
    kept = make_agent(store, "Zqkept Scout", hidden)
    visible = make_agent(store, "Plain Helper")
    archive.trash(store, archive.Trash(sessions=[hidden], trashed=True))
    for wanted in (kept, "Zqkept Scout", "zqkept scout"):
        messages = refused(store, wanted)
        assert all("There is no agent" in text for text in messages)
        assert all("archived" not in text for text in messages)
    assert lookups(store, "Plain Helper") == [visible, visible]
    archive.trash(store, archive.Trash(sessions=[hidden], trashed=False))
    assert lookups(store, "Zqkept Scout") == [kept, kept]
