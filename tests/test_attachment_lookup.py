"""One attachment lookup per history walk, not one connection per message."""

from contextlib import closing

from test_attachment_turns import Provider, upload

from pi.content import attachment_turns, attachments
from pi.conversation.store import Store
from pi.models.routing import Router
from pi.turns.loop import Loop


def bind_message(store, sid, identity):
    message = store.append_message(sid, "user", "attached message")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        attachments.bind(db, sid, message["id"], [identity])
        db.commit()
    return message["id"]


def test_attached_message_ids_returns_exactly_the_bound_ids(tmp_path):
    with closing(Store(tmp_path / "a.db")) as store:
        sid = store.create_session()
        bound = bind_message(store, sid, upload(store, sid))
        plain = store.append_message(sid, "user", "plain message")["id"]
        assert attachment_turns.attached_message_ids(store, [bound, plain, "absent"]) == {bound}


def test_more_than_500_ids_are_chunked(tmp_path):
    with closing(Store(tmp_path / "a.db")) as store:
        sid = store.create_session()
        first = bind_message(store, sid, upload(store, sid))
        second = bind_message(store, sid, upload(store, sid))
        ids = [first] + [f"absent-{n}" for n in range(1200)] + [second]
        assert attachment_turns.attached_message_ids(store, ids) == {first, second}


def test_empty_input_opens_no_connection(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "a.db")) as store:

        def explode(*args):
            raise AssertionError("a connection was opened for no message ids")

        monkeypatch.setattr(store, "_connect", explode)
        assert attachment_turns.attached_message_ids(store, []) == set()


def test_history_preparation_calls_attachment_context_once(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "a.db")) as store:
        sid = store.create_session()
        identity = upload(store, sid)
        for number in range(30):
            store.append_message(sid, "user", f"earlier message {number}")
        provider = Provider()
        loop = Loop(store, Router(local_provider=provider, local_model="local"))
        calls = []
        original = attachment_turns.context

        def counting(store_arg, session_id, message_id, privacy):
            calls.append(message_id)
            return original(store_arg, session_id, message_id, privacy)

        monkeypatch.setattr(attachment_turns, "context", counting)
        result = loop.run_turn(
            sid, "Read this", request_id="attachment_request_1", attachment_ids=[identity]
        )
        assert len(calls) == 1
        assert any("Attachment content 42" in message.content for message in provider.calls[0])
        assert store.get_turn(result["turn_id"])["status"] == "complete"
