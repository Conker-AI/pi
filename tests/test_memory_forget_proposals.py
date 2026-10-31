"""Forgetting one memory scrubs every proposal that quoted or targeted it.

A proposal keeps the memory's old text (baseline), the proposed text and the reason. Without the
scrub, a verified forget left all three readable in memory_proposals.
"""

import json
from contextlib import closing

import httpx
import pytest

from pi.agents import profiles as agents
from pi.conversation.store import Store
from pi.memory import forget as memory_forget
from pi.memory import proposals as p
from pi.memory.corrections import Client
from tests.test_memory_proposals import Remote

OLD = "synthetic-old-memory-text-7714"
PROPOSED = "synthetic-proposed-text-3390"


class ForgetRemote(Remote):
    def handle(self, request):
        if request.method == "PUT" and "/forget/" in request.url.path:
            body = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "request_id": request.url.path.rsplit("/", 1)[-1],
                    "memory_id": body["memory_id"],
                    "agent_id": "companion-memory",
                    "revision": body["expected_revision"],
                    "status": "forgotten",
                    "index_removal": "removed",
                },
            )
        return super().handle(request)


def _proposal(store, client, sid, message, request_id, memory_id, text):
    return p.create(
        store,
        p.Create(
            request_id=request_id,
            session_id=sid,
            memory_id=memory_id,
            expected_memory_revision=3,
            text=text,
            reason=text,
            basis="stated",
            source_message_ids=[message["id"]],
        ),
        client,
    )


def test_forgetting_a_memory_scrubs_its_proposals_and_keeps_others(tmp_path):
    remote = ForgetRemote()
    remote.text = OLD
    with (
        closing(Store(tmp_path / "pi.db")) as store,
        closing(
            Client(
                "https://memory.test",
                "k" * 32,
                "companion-memory",
                transport=httpx.MockTransport(remote.handle),
            )
        ) as client,
    ):
        sid = store.create_session()
        message = store.append_message(sid, "user", "I changed my mind.")
        _proposal(store, client, sid, message, "proposal_pending_01", "mem_1", PROPOSED)
        _proposal(store, client, sid, message, "proposal_rejected_1", "mem_1", PROPOSED)
        p.decide(store, "proposal_rejected_1", p.Decision(decision="reject"), client)
        _proposal(store, client, sid, message, "proposal_other_001", "mem_2", "Keep this one")

        memory_forget.forget(
            store,
            client,
            memory_forget.Forget(
                request_id="forget_request_0001", memory_id="mem_1", expected_revision=3
            ),
        )

        for identity in ("proposal_pending_01", "proposal_rejected_1"):
            with pytest.raises(agents.AgentError, match="unavailable"):
                p.get(store, identity)
        assert [row["id"] for row in p.list_all(store, sid)] == ["proposal_other_001"]
        assert p.get(store, "proposal_other_001")["desired"] == "Keep this one"
        with store._connect() as db:
            rows = db.execute(
                "SELECT baseline,desired,reason,source_ids,receipt,state FROM memory_proposals "
                "WHERE memory_id='mem_1'"
            ).fetchall()
            fixed = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='memory_proposals_fixed'"
            ).fetchone()
        assert [tuple(row) for row in rows] == [(None, None, None, "[]", None, "forgotten")] * 2
        assert fixed is not None  # The immutability trigger is back after the scrub.
