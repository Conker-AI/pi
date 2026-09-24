from contextlib import closing

import pytest

from pi import attachments, session_settings, submissions
from pi.loop import Loop
from pi.providers import Completion
from pi.routing import Router
from pi.store import Store


class Provider:
    name = "local"

    def __init__(self):
        self.calls = []

    def complete(self, messages, *, model):
        self.calls.append(messages)
        return Completion(text="Read it", model=model, provider=self.name)


def upload(store, sid, kind="text/plain"):
    return attachments.upload(
        store,
        sid,
        attachments.Metadata(name="note.txt", type=kind),
        b"Attachment content 42",
        resolve=session_settings.source_privacy,
    )["id"]


def test_attachment_reaches_model_bound_to_exact_input(tmp_path):
    with closing(Store(tmp_path / "a.db")) as store:
        sid = store.create_session()
        identity = upload(store, sid)
        provider = Provider()
        loop = Loop(store, Router(local_provider=provider, local_model="local"))
        result = loop.run_turn(
            sid, "Read this", request_id="attachment_request_1", attachment_ids=[identity]
        )
        assert any("Attachment content 42" in message.content for message in provider.calls[0])
        assert store.get_turn(result["turn_id"])["status"] == "complete"
        input_message = next(
            message for message in store.messages(sid) if message["role"] == "user"
        )
        assert input_message["attachments"][0]["id"] == identity
        assert store.get_message(input_message["id"])["attachments"] == input_message["attachments"]
        loop.run_turn(
            sid, "Read this", request_id="attachment_request_1", attachment_ids=[identity]
        )
        assert len(provider.calls) == 1
        with store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM message_attachments").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM attachment_reservations").fetchone()[0] == 0


def test_foreign_or_unsupported_attachment_never_creates_submission(tmp_path):
    with closing(Store(tmp_path / "a.db")) as store:
        sid, other = store.create_session(), store.create_session()
        for identity in (upload(store, other), upload(store, sid, "application/pdf")):
            with pytest.raises(submissions.SubmissionError):
                submissions.reserve(
                    store, "attachment_request_1", sid, "Read this", {}, attachment_ids=[identity]
                )
        with store._connect() as db:
            assert db.execute("SELECT COUNT(*) FROM turn_submissions").fetchone()[0] == 0


def test_preparation_holds_file_until_failed(tmp_path):
    with closing(Store(tmp_path / "a.db")) as store:
        sid = store.create_session()
        identity = upload(store, sid)
        request = "attachment_request_1"
        submissions.reserve(store, request, sid, "Read this", {}, attachment_ids=[identity])
        with pytest.raises(attachments.AttachmentError):
            attachments.remove(store, sid, identity, resolve=session_settings.source_privacy)
        submissions.fail_preparation(store, request)
        attachments.remove(store, sid, identity, resolve=session_settings.source_privacy)


def test_private_upload_cannot_be_sent_after_lowering_privacy(tmp_path):
    with closing(Store(tmp_path / "a.db")) as store:
        sid = store.create_session()
        for revision, private in ((0, True), (1, False)):
            session_settings.save(
                store,
                sid,
                session_settings.Update(
                    expected_revision=revision,
                    settings=session_settings.Settings(
                        agentId="companion",
                        privacy=session_settings.Privacy(
                            memoryDisabled=private, harnessDisabled=False
                        ),
                    ),
                ),
            )
            if private:
                identity = upload(store, sid)
        with pytest.raises(submissions.SubmissionError, match="privacy modes"):
            submissions.reserve(
                store, "attachment_request_1", sid, "Read", {}, attachment_ids=[identity]
            )


def test_owner_stop_releases_reserved_upload_for_new_submission(tmp_path):
    from pi import turn_control

    with closing(Store(tmp_path / "a.db")) as store:
        sid = store.create_session()
        identity = upload(store, sid)
        submissions.reserve(
            store, "cancel_attachment_001", sid, "Read this", {}, attachment_ids=[identity]
        )
        turn_control.cancel_submission(store, "cancel_attachment_001")
        submissions.reserve(
            store, "cancel_attachment_002", sid, "Read this", {}, attachment_ids=[identity]
        )
        with store._connect() as db:
            rows = db.execute("SELECT request_id FROM attachment_reservations").fetchall()
            assert [row[0] for row in rows] == ["cancel_attachment_002"]
