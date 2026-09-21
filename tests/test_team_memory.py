from contextlib import closing

import pytest

from pi import session_settings
from pi import team_execution as teams
from pi.loop import Loop
from pi.memory import Memory
from pi.routing import Router
from pi.store import Store
from tests.test_team_execution import Recorder, setup, start


@pytest.mark.parametrize("disabled", [False, True])
def test_team_reads_source_conversation_without_ingesting_team_messages(tmp_path, disabled):
    class Client:
        def __init__(self):
            self.calls = []

        def retrieve(self, query, **options):
            self.calls.append(options)
            return {"memories": [], "retrieval": {}}

    with closing(Store(tmp_path / "test.db")) as store:
        sid = store.create_session()
        record, agent = setup(store, memory_scope="conversation")
        run = start(store, record, sid)
        if disabled:
            session_settings.save(
                store,
                sid,
                session_settings.Update(
                    expected_revision=0,
                    settings=session_settings.Settings(
                        agentId="companion",
                        privacy=session_settings.Privacy(
                            memoryDisabled=True, harnessDisabled=False
                        ),
                    ),
                ),
            )
        client = Client()
        runtime = Loop(
            store,
            Router(local_provider=Recorder()),
            memory=Memory(store, read_clients={agent["id"]: client}),
        )
        teams.execute(
            store,
            runtime,
            run["id"],
            teams.Step(
                request_id="memory_step_request_01",
                expected_revision=run["revision"],
                roleId="first",
            ),
        )
        assert client.calls == (
            [] if disabled else [{"scope": "conversation", "session_id": sid, "memory_ids": []}]
        )
        with store._connect() as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM memory_outbox o JOIN messages m ON m.id=o.message_id "
                    "JOIN team_steps s ON s.session_id=m.session_id"
                ).fetchone()[0]
                == 0
            )
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM message_privacy p JOIN messages m ON m.id=p.message_id "
                    "JOIN team_steps s ON s.session_id=m.session_id WHERE p.memory_disabled!=1"
                ).fetchone()[0]
                == 0
            )
