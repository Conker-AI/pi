"""Test every attachment schema trigger fires and allowed paths work."""

import sqlite3
from pathlib import Path

import pytest

from pi.content import attachments as a
from pi.conversation import settings as session_settings
from pi.conversation import submissions
from pi.conversation.store import Store

resolve = session_settings.source_privacy


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "attachment_triggers.db")
    yield value
    value.close()


def upload(store, session, raw=b"plain text", name="note.txt", media_type="text/plain"):
    return a.upload(
        store, session, a.Metadata(name=name, type=media_type, lastModified=123), raw, resolve
    )


def test_all_trigger_messages_are_tested():
    """Ensure every RAISE message in the schema has a corresponding test."""
    # Extract all RAISE(ABORT,'...') messages from schema
    import re

    from pi.content.attachment_schema import SCHEMA

    trigger_messages = set(re.findall(r"RAISE\(ABORT,'([^']+)'\)", SCHEMA))

    # Read this file's source and find all match= patterns in pytest.raises
    test_file = Path(__file__).read_text()
    tested_messages = set(re.findall(r'match="([^"]+)"', test_file))

    # Ensure every trigger message is tested
    untested = trigger_messages - tested_messages
    assert not untested, f"Triggers without tests: {untested}"


class TestAttachmentReservationsTriggers:
    """Test attachment_reservations table triggers."""

    @pytest.fixture
    def reservation_setup(self, store):
        session = store.create_session()
        attachment = upload(store, session)
        request_id = "test_reservation_123"
        submissions.reserve(store, request_id, session, "preparing", {})
        with store._connect() as db:
            db.execute(
                "INSERT INTO attachment_reservations VALUES(?,?)", (request_id, attachment["id"])
            )
        return store, session, attachment["id"], request_id

    def test_attachment_reservations_no_update(self, reservation_setup):
        store, _session, _attachment_id, _request_id = reservation_setup
        with (
            store._connect() as db,
            pytest.raises(
                sqlite3.IntegrityError, match="attachment reservations have fixed identities"
            ),
        ):
            db.execute("UPDATE attachment_reservations SET attachment_id=?", ("other_id",))

    def test_attachment_reservations_no_replace(self, reservation_setup):
        store, _session, attachment_id, request_id = reservation_setup
        with (
            store._connect() as db,
            pytest.raises(
                sqlite3.IntegrityError, match="attachment reservations have fixed identities"
            ),
        ):
            db.execute(
                "INSERT INTO attachment_reservations VALUES(?,?)", (request_id, attachment_id)
            )

    def test_attachment_reservations_valid_invalid_attachment(self, store):
        session = store.create_session()
        attachment = upload(store, session)
        request_id = "test_reservation_456"
        submissions.reserve(store, request_id, session, "preparing", {})

        # Remove attachment to make it unavailable
        a.remove(store, session, attachment["id"], resolve)

        with (
            store._connect() as db,
            pytest.raises(
                sqlite3.IntegrityError,
                match="reservation requires an available originating attachment",
            ),
        ):
            db.execute(
                "INSERT INTO attachment_reservations VALUES(?,?)",
                (request_id, attachment["id"]),
            )

    def test_attachment_reservations_valid_wrong_state(self, store):
        session = store.create_session()
        attachment = upload(store, session)
        request_id = "test_reservation_789"
        submissions.reserve(store, request_id, session, "pending", {})  # Wrong state

        # Change state to something other than 'preparing' to trigger the validation
        with store._connect() as db:
            db.execute(
                "UPDATE turn_submissions SET state='bound' WHERE request_id=?", (request_id,)
            )

        with (
            store._connect() as db,
            pytest.raises(
                sqlite3.IntegrityError,
                match="reservation requires an available originating attachment",
            ),
        ):
            db.execute(
                "INSERT INTO attachment_reservations VALUES(?,?)",
                (request_id, attachment["id"]),
            )

    def test_attachment_reservations_allowed_path(self, store):
        session = store.create_session()
        attachment = upload(store, session)
        request_id = "test_reservation_allowed"
        submissions.reserve(store, request_id, session, "preparing", {})

        # This should work
        with store._connect() as db:
            db.execute(
                "INSERT INTO attachment_reservations VALUES(?,?)", (request_id, attachment["id"])
            )
            db.commit()


class TestAttachmentRemovalsTriggers:
    """Test attachment_removals table triggers."""

    @pytest.fixture
    def removal_setup(self, store):
        session = store.create_session()
        attachment = upload(store, session)
        receipt = a.remove(store, session, attachment["id"], resolve)
        return store, session, attachment["id"], receipt["id"]

    @pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
    def test_attachment_removals_immutable(self, removal_setup, operation):
        store, _session, _attachment_id, removal_id = removal_setup
        with (
            store._connect() as db,
            pytest.raises(
                sqlite3.IntegrityError, match="attachment removal receipts are immutable"
            ),
        ):
            if operation == "UPDATE":
                db.execute("UPDATE attachment_removals SET removed_at=0 WHERE id=?", (removal_id,))
            else:
                db.execute("DELETE FROM attachment_removals WHERE id=?", (removal_id,))

    def test_attachment_removals_no_replace_id(self, removal_setup):
        store, _session, attachment_id, removal_id = removal_setup
        with (
            store._connect() as db,
            pytest.raises(
                sqlite3.IntegrityError, match="attachment removal receipts are immutable"
            ),
        ):
            db.execute(
                "INSERT INTO attachment_removals VALUES(?,?,?,?)",
                (removal_id, attachment_id, 123.0, "owner-removed"),
            )

    def test_attachment_removals_no_replace_attachment_id(self, store):
        session = store.create_session()
        attachment1 = upload(store, session)
        upload(store, session)
        a.remove(store, session, attachment1["id"], resolve)

        with (
            store._connect() as db,
            pytest.raises(
                sqlite3.IntegrityError, match="attachment removal receipts are immutable"
            ),
        ):
            db.execute(
                "INSERT INTO attachment_removals VALUES(?,?,?,?)",
                ("new_id", attachment1["id"], 123.0, "owner-removed"),
            )

    def test_attachment_removals_allowed_path(self, store):
        session = store.create_session()
        attachment = upload(store, session)
        # This should work
        receipt = a.remove(store, session, attachment["id"], resolve)
        assert receipt["reason"] == "owner-removed"


class TestMessageAttachmentSetsTriggers:
    """Test message_attachment_sets table triggers."""

    @pytest.fixture
    def message_set_setup(self, store):
        session = store.create_session()
        message = store.append_message(session, "user", "test message")
        with store._connect() as db:
            db.execute("INSERT INTO message_attachment_sets VALUES(?,?)", (message["id"], "[]"))
        return store, session, message["id"]

    @pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
    def test_message_attachment_sets_immutable(self, message_set_setup, operation):
        store, _session, message_id = message_set_setup
        with (
            store._connect() as db,
            pytest.raises(sqlite3.IntegrityError, match="message attachment sets are immutable"),
        ):
            if operation == "UPDATE":
                db.execute(
                    "UPDATE message_attachment_sets SET attachment_ids='[]' WHERE message_id=?",
                    (message_id,),
                )
            else:
                db.execute("DELETE FROM message_attachment_sets WHERE message_id=?", (message_id,))

    def test_message_attachment_sets_no_replace(self, message_set_setup):
        store, _session, message_id = message_set_setup
        with (
            store._connect() as db,
            pytest.raises(sqlite3.IntegrityError, match="message attachment sets are immutable"),
        ):
            db.execute("INSERT INTO message_attachment_sets VALUES(?,?)", (message_id, "[]"))

    def test_message_attachment_sets_allowed_path(self, store):
        session = store.create_session()
        message = store.append_message(session, "user", "test message")
        # This should work - first insertion
        with store._connect() as db:
            db.execute("INSERT INTO message_attachment_sets VALUES(?,?)", (message["id"], "[]"))
            db.commit()


class TestMessageAttachmentsTriggers:
    """Test message_attachments table triggers."""

    @pytest.fixture
    def message_attachment_setup(self, store):
        session = store.create_session()
        attachment = upload(store, session)
        message = store.append_message(session, "user", "test message")
        with store._connect() as db:
            db.execute(
                "INSERT INTO message_attachment_sets VALUES(?,?)",
                (message["id"], f'["{attachment["id"]}"]'),
            )
            db.execute(
                "INSERT INTO message_attachments VALUES(?,?,?)",
                (message["id"], 0, attachment["id"]),
            )
        return store, session, message["id"], attachment["id"]

    @pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
    def test_message_attachments_immutable(self, message_attachment_setup, operation):
        store, _session, message_id, _attachment_id = message_attachment_setup
        with (
            store._connect() as db,
            pytest.raises(
                sqlite3.IntegrityError, match="message attachment bindings are immutable"
            ),
        ):
            if operation == "UPDATE":
                db.execute(
                    "UPDATE message_attachments SET attachment_id='other' WHERE message_id=?",
                    (message_id,),
                )
            else:
                db.execute("DELETE FROM message_attachments WHERE message_id=?", (message_id,))

    def test_message_attachments_no_replace_position(self, message_attachment_setup):
        store, _session, message_id, _attachment_id = message_attachment_setup
        with (
            store._connect() as db,
            pytest.raises(
                sqlite3.IntegrityError, match="message attachment bindings are immutable"
            ),
        ):
            db.execute("INSERT INTO message_attachments VALUES(?,?,?)", (message_id, 0, "other_id"))

    def test_message_attachments_no_replace_attachment_id(self, message_attachment_setup):
        store, _session, message_id, attachment_id = message_attachment_setup
        with (
            store._connect() as db,
            pytest.raises(
                sqlite3.IntegrityError, match="message attachment bindings are immutable"
            ),
        ):
            db.execute(
                "INSERT INTO message_attachments VALUES(?,?,?)", (message_id, 1, attachment_id)
            )

    def test_message_attachments_valid_wrong_session(self, store):
        session1, session2 = store.create_session(), store.create_session()
        attachment = upload(store, session1)
        message = store.append_message(session2, "user", "test message")

        with store._connect() as db:
            db.execute(
                "INSERT INTO message_attachment_sets VALUES(?,?)",
                (message["id"], f'["{attachment["id"]}"]'),
            )
            with pytest.raises(
                sqlite3.IntegrityError,
                match="attachment and user message must share an available session",
            ):
                db.execute(
                    "INSERT INTO message_attachments VALUES(?,?,?)",
                    (message["id"], 0, attachment["id"]),
                )

    def test_message_attachments_valid_purged_attachment(self, store):
        session = store.create_session()
        attachment = upload(store, session)
        message = store.append_message(session, "user", "test message")
        a.remove(store, session, attachment["id"], resolve)  # Purge it

        with store._connect() as db:
            db.execute(
                "INSERT INTO message_attachment_sets VALUES(?,?)",
                (message["id"], f'["{attachment["id"]}"]'),
            )
            with pytest.raises(
                sqlite3.IntegrityError,
                match="attachment and user message must share an available session",
            ):
                db.execute(
                    "INSERT INTO message_attachments VALUES(?,?,?)",
                    (message["id"], 0, attachment["id"]),
                )

    def test_message_attachments_valid_assistant_role(self, store):
        session = store.create_session()
        attachment = upload(store, session)
        message = store.append_message(session, "assistant", "test message")  # Wrong role

        with store._connect() as db:
            db.execute(
                "INSERT INTO message_attachment_sets VALUES(?,?)",
                (message["id"], f'["{attachment["id"]}"]'),
            )
            with pytest.raises(
                sqlite3.IntegrityError,
                match="attachment and user message must share an available session",
            ):
                db.execute(
                    "INSERT INTO message_attachments VALUES(?,?,?)",
                    (message["id"], 0, attachment["id"]),
                )

    def test_message_attachments_exact_set_mismatch(self, store):
        session = store.create_session()
        attachment1 = upload(store, session)
        attachment2 = upload(store, session)
        message = store.append_message(session, "user", "test message")

        with store._connect() as db:
            # Set expects attachment1 but we try to insert attachment2
            db.execute(
                "INSERT INTO message_attachment_sets VALUES(?,?)",
                (message["id"], f'["{attachment1["id"]}"]'),
            )
            with pytest.raises(
                sqlite3.IntegrityError, match="attachment must match the sealed message set"
            ):
                db.execute(
                    "INSERT INTO message_attachments VALUES(?,?,?)",
                    (message["id"], 0, attachment2["id"]),
                )

    def test_message_attachments_allowed_path(self, store):
        session = store.create_session()
        attachment = upload(store, session)
        message = store.append_message(session, "user", "test message")

        # This should work - proper binding through API
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            a.bind(db, session, message["id"], [attachment["id"]])
            db.commit()


class TestAttachmentsTriggers:
    """Test attachments table triggers."""

    @pytest.fixture
    def attachment_setup(self, store):
        session = store.create_session()
        attachment = upload(store, session)
        return store, session, attachment["id"]

    def test_attachments_no_replace(self, attachment_setup):
        store, session, attachment_id = attachment_setup
        with (
            store._connect() as db,
            pytest.raises(sqlite3.IntegrityError, match="attachment identities are immutable"),
        ):
            db.execute(
                "INSERT INTO attachments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attachment_id,
                    session,
                    "note.txt",
                    "text/plain",
                    10,
                    123.0,
                    123.0,
                    b"data",
                    "hash",
                    "text",
                    "processed",
                    "reason",
                    "private",
                    0,
                    0,
                ),
            )

    def test_attachments_no_update_invalid_change(self, attachment_setup):
        store, _session, attachment_id = attachment_setup
        with (
            store._connect() as db,
            pytest.raises(
                sqlite3.IntegrityError, match="attachment bytes and provenance are immutable"
            ),
        ):
            db.execute("UPDATE attachments SET name='changed.txt' WHERE id=?", (attachment_id,))

    def test_attachments_no_delete(self, attachment_setup):
        store, _session, attachment_id = attachment_setup
        with (
            store._connect() as db,
            pytest.raises(sqlite3.IntegrityError, match="attachment identities are permanent"),
        ):
            db.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))

    def test_attachments_valid_source_closed_session(self, store):
        session = store.create_session()
        store.close_session(session, "closed")

        with (
            store._connect() as db,
            pytest.raises(sqlite3.IntegrityError, match="attachment requires an active session"),
        ):
            db.execute(
                "INSERT INTO attachments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "new_id",
                    session,
                    "note.txt",
                    "text/plain",
                    10,
                    123.0,
                    123.0,
                    b"data",
                    "hash",
                    "text",
                    "processed",
                    "reason",
                    "private",
                    0,
                    0,
                ),
            )

    def test_attachments_valid_source_forgotten_session(self, store):
        session = store.create_session()
        # Mark as forgotten
        with store._connect() as db:
            db.execute(
                "INSERT INTO forgetting_receipts VALUES('test_receipt',?,0,'confirmed',0,0)",
                (session,),
            )
            db.execute("INSERT INTO forgotten_sessions VALUES(?,'test_receipt')", (session,))

        with (
            store._connect() as db,
            pytest.raises(sqlite3.IntegrityError, match="attachment requires an active session"),
        ):
            db.execute(
                "INSERT INTO attachments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "new_id",
                    session,
                    "note.txt",
                    "text/plain",
                    10,
                    123.0,
                    123.0,
                    b"data",
                    "hash",
                    "text",
                    "processed",
                    "reason",
                    "private",
                    0,
                    0,
                ),
            )

    def test_attachments_allowed_path(self, store):
        session = store.create_session()
        # This should work - normal upload
        attachment = upload(store, session)
        assert attachment["id"] is not None

    def test_attachments_allowed_purge_path(self, attachment_setup):
        store, session, _attachment_id = attachment_setup
        # Purge is allowed - this is the special case in the trigger
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            a.redact(db, [session])
            db.commit()
