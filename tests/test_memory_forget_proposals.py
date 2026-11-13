"""Old memory proposal rows are scrubbed when their chat or their memory is forgotten.

Memory proposals are retired (pi/memory/proposals.py): nothing writes or lists them any more,
but installs from before may still hold rows with a memory's old text (baseline), the proposed
text and the reason. Without the scrub, forgetting left all three readable in memory_proposals.
"""

import json
from contextlib import closing

import httpx

from pi.conversation import forgetting
from pi.conversation.store import Store
from pi.memory import forget as memory_forget
from pi.memory.corrections import Client

OLD = "synthetic-old-memory-text-7714"
PROPOSED = "synthetic-proposed-text-3390"
AGENT = "companion-memory"


def _remote(request):
    if request.method == "PUT" and "/forget/" in request.url.path:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "request_id": request.url.path.rsplit("/", 1)[-1],
                "memory_id": body["memory_id"],
                "agent_id": AGENT,
                "revision": body["expected_revision"],
                "status": "forgotten",
                "index_removal": "removed",
            },
        )
    identity = request.url.path.rsplit("/", 1)[-1]
    return httpx.Response(
        200,
        json={
            "id": identity,
            "agent_id": AGENT,
            "revision": 3,
            "text": OLD,
            "source_type": "user",
            "confidence": "high",
        },
    )


def _old_row(store, identity, session, memory_id, text, state="pending"):
    """A row as the retired `/memory-proposals` route wrote it."""
    with store._connect() as db:
        db.execute(
            "INSERT INTO memory_proposals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)",
            (
                identity,
                session,
                "hash-" + identity,
                memory_id,
                AGENT,
                3,
                json.dumps({"id": memory_id, "revision": 3, "text": OLD}),
                text,
                text,
                "stated",
                json.dumps(["msg_1"]),
                state,
                1700000000.0,
            ),
        )


def _rows(store, where):
    with store._connect() as db:
        return [
            tuple(row)
            for row in db.execute(
                "SELECT baseline,desired,reason,source_ids,receipt,state FROM memory_proposals "
                "WHERE " + where + " ORDER BY id"
            )
        ]


def _trigger_back(store):
    with store._connect() as db:
        return db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='memory_proposals_fixed'"
        ).fetchone()


def test_forgetting_a_memory_scrubs_its_proposals_and_keeps_others(tmp_path):
    with (
        closing(Store(tmp_path / "pi.db")) as store,
        closing(
            Client("https://memory.test", "k" * 32, AGENT, transport=httpx.MockTransport(_remote))
        ) as client,
    ):
        sid = store.create_session()
        _old_row(store, "proposal_pending_01", sid, "mem_1", PROPOSED)
        _old_row(store, "proposal_rejected_1", sid, "mem_1", PROPOSED, state="rejected")
        _old_row(store, "proposal_other_001", sid, "mem_2", "Keep this one")

        memory_forget.forget(
            store,
            client,
            memory_forget.Forget(
                request_id="forget_request_0001", memory_id="mem_1", expected_revision=3
            ),
        )

        scrubbed = (None, None, None, "[]", None, "forgotten")
        assert _rows(store, "memory_id='mem_1'") == [scrubbed] * 2
        assert [row[1] for row in _rows(store, "memory_id='mem_2'")] == ["Keep this one"]
        assert _trigger_back(store) is not None  # The immutability trigger is back.


def test_forgetting_a_chat_scrubs_its_old_proposals_from_every_file(tmp_path):
    path = tmp_path / "pi.db"
    secret = "proposal-private-content-8812937"
    with closing(Store(path)) as store:
        sid = store.create_session()
        kept = store.create_session()
        store.append_message(sid, "user", "a message so the chat can be forgotten")
        _old_row(store, "proposal_in_chat_01", sid, "mem_1", secret)
        _old_row(store, "proposal_in_chat_02", sid, "mem_1", secret, state="unknown")
        _old_row(store, "proposal_elsewhere1", kept, "mem_2", "Keep this one")

    forgetting.forget(path, sid, forgetting.preview(path, sid)["confirmation"])

    with closing(Store(path)) as store:
        scrubbed = (None, None, None, "[]", None, "forgotten")
        assert _rows(store, f"session_id='{sid}'") == [scrubbed] * 2
        assert [row[1] for row in _rows(store, f"session_id='{kept}'")] == ["Keep this one"]
        assert _trigger_back(store) is not None
    for file in tmp_path.iterdir():
        if file.is_file():
            assert secret.encode() not in file.read_bytes()
