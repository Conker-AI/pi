"""Call privacy cannot be relaxed through ordinary conversation settings."""

from contextlib import closing

import pytest

from pi import agents, calls, session_settings
from pi.store import Store


def test_generic_settings_cannot_bypass_call_privacy(tmp_path):
    with closing(Store(tmp_path / "call.db")) as store:
        source = store.create_session()
        call = calls.start(
            store,
            calls.Start(
                request_id="start_privacy_boundary",
                conversationId=source,
                privacy=calls.Privacy(memory=True, harness=True),
            ),
        )
        current = session_settings.load(store, call["sessionId"])
        with pytest.raises(agents.AgentError, match="call controls"):
            session_settings.save(
                store,
                call["sessionId"],
                session_settings.Update(
                    expected_revision=current["revision"],
                    settings=session_settings.Settings(
                        agentId="companion",
                        privacy=session_settings.Privacy(
                            memoryDisabled=False, harnessDisabled=False
                        ),
                    ),
                ),
            )
        assert session_settings.load(store, call["sessionId"]) == current
