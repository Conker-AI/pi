"""Tests for disk-based attachment storage without size limits."""

import hashlib

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from pi.api.routes.attachments import router
from pi.content import attachment_files
from pi.content import attachments as a
from pi.conversation import settings as session_settings
from pi.conversation.store import Store

resolve = session_settings.source_privacy


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "attachments.db")
    yield value
    value.close()


def upload_file(store, session, raw=b"plain text", name="note.txt", media_type="text/plain"):
    """Helper to upload a file using the new disk-based system."""
    return a.upload(
        store, session, a.Metadata(name=name, type=media_type, lastModified=123), raw, resolve
    )


def test_new_upload_creates_disk_file_and_row(store):
    """Test that a new upload creates a file on disk with correct metadata."""
    session = store.create_session()
    raw = b"test content for disk storage"
    saved = upload_file(store, session, raw)

    # Check database row
    assert saved["storage"] == "disk"
    assert saved["size"] == len(raw)
    assert saved["sha256"] == hashlib.sha256(raw).hexdigest()

    # Check file exists on disk
    file_path = attachment_files.blob_dir(store) / saved["id"]
    assert file_path.exists()
    assert file_path.read_bytes() == raw

    # Check download returns same bytes
    _, downloaded = a.download(store, session, saved["id"], resolve)
    assert downloaded == raw


def test_large_upload_accepted_and_stored(store):
    """Test that a 30 MiB upload is accepted and stored without text extraction."""
    session = store.create_session()

    # Generate 30 MiB of data
    raw = b"x" * (30 * 1024 * 1024)
    saved = upload_file(
        store, session, raw, name="large.bin", media_type="application/octet-stream"
    )

    # Check it was stored
    assert saved["size"] == len(raw)
    assert saved["processing"]["status"] == "unsupported"
    assert "Too large to read as text" in saved["processing"]["reason"]

    # Check file exists and is downloadable
    file_path = attachment_files.blob_dir(store) / saved["id"]
    assert file_path.exists()
    assert file_path.read_bytes() == raw

    _, downloaded = a.download(store, session, saved["id"], resolve)
    assert downloaded == raw


def test_small_text_file_extracted(store):
    """Test that small text files are still extracted."""
    session = store.create_session()
    raw = b"small text content"
    saved = upload_file(store, session, raw, name="small.txt", media_type="text/plain")

    assert saved["processing"]["status"] == "extracted"
    assert a.extract(store, session, saved["id"], resolve)["text"] == raw.decode()


def test_eleven_files_bind_to_message(store):
    """Test that eleven files can be bound to one message."""
    session = store.create_session()

    # Create eleven small files
    file_ids = []
    for i in range(11):
        saved = upload_file(store, session, f"content {i}".encode(), name=f"file{i}.txt")
        file_ids.append(saved["id"])

    # Bind all to a message
    message = store.append_message(session, "user", "message with many files")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        a.bind(db, session, message["id"], file_ids)
        db.commit()

    # Check all are bound
    with store._connect() as db:
        views = a.message_views(db, message["id"], resolve)
        assert len(views) == 11


def test_chat_with_150_uploads_accepted(store):
    """Test that a chat with 150 uploads is accepted."""
    session = store.create_session()

    # Create 150 tiny files
    file_ids = []
    for i in range(150):
        saved = upload_file(store, session, b"x", name=f"tiny{i}.txt")
        file_ids.append(saved["id"])

    # Check all are listed
    listing = a.listing(store, session, resolve)
    assert len(listing) == 150


def test_legacy_blob_row_still_works(store):
    """Test that legacy database rows still work."""
    session = store.create_session()
    raw = b"legacy content"
    sha256 = hashlib.sha256(raw).hexdigest()

    # Insert a legacy row directly
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        identity = "attachment_legacy_" + "0" * 32
        db.execute(
            "INSERT INTO attachments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
            (
                identity,
                session,
                "legacy.txt",
                "text/plain",
                len(raw),
                123,
                1234567890,
                raw,  # BLOB content
                sha256,
                "legacy content",
                "extracted",
                "UTF-8 plaintext",
                '{"memoryDisabled": false, "harnessDisabled": false}',
                "database",  # Legacy storage type
            ),
        )
        db.commit()

    # Check it can be downloaded and extracted
    _, downloaded = a.download(store, session, identity, resolve)
    assert downloaded == raw

    extracted = a.extract(store, session, identity, resolve)
    assert extracted["text"] == "legacy content"


def test_corrupt_disk_file_integrity_failure(store):
    """Test that corrupting a disk file causes integrity failure."""
    session = store.create_session()
    raw = b"original content"
    saved = upload_file(store, session, raw)

    # Corrupt the file
    file_path = attachment_files.blob_dir(store) / saved["id"]
    file_path.write_bytes(b"corrupted content")

    # Download should fail
    with pytest.raises(a.AttachmentError) as exc:
        a.download(store, session, saved["id"], resolve)
    assert exc.value.detail["code"] == "integrity_failure"


def test_missing_disk_file_integrity_failure(store):
    """Test that deleting a disk file causes integrity failure."""
    session = store.create_session()
    raw = b"content to be deleted"
    saved = upload_file(store, session, raw)

    # Delete the file
    file_path = attachment_files.blob_dir(store) / saved["id"]
    file_path.unlink()

    # Download should fail
    with pytest.raises(a.AttachmentError) as exc:
        a.download(store, session, saved["id"], resolve)
    assert exc.value.detail["code"] == "integrity_failure"


def test_failed_insert_leaves_no_files(store):
    """Test that a failed insert leaves no files behind."""
    session = "nonexistent_session"  # This will cause the insert to fail

    # Try to upload (should fail)
    raw = b"content"
    with pytest.raises(a.AttachmentError):
        upload_file(store, session, raw)

    # Check no files were left behind in incoming directory
    incoming_dir = attachment_files.blob_dir(store) / ".incoming"
    assert not incoming_dir.exists() or not any(incoming_dir.iterdir())

    # Check no attachment files were left behind (only .incoming directory should exist)
    attachments_dir = attachment_files.blob_dir(store)
    for item in attachments_dir.iterdir():
        assert item.name == ".incoming"


def test_http_upload_mismatched_content_length(store):
    """Test HTTP upload with mismatched content-length."""

    def authorize(key: str | None = Header(default=None)):
        if key != "owner":
            raise HTTPException(403)

    app = FastAPI()
    app.include_router(router(lambda: store, authorize, resolve))
    client = TestClient(app)
    session = store.create_session()
    url = f"/sessions/{session}/attachments"

    # Upload with wrong content-length
    response = client.post(
        url,
        headers={
            "key": "owner",
            "content-length": "100",  # Wrong length
        },
        params={"name": "test.txt"},
        content=b"short content",
    )
    assert response.status_code == 400

    # Check no files were left behind
    incoming_dir = attachment_files.blob_dir(store) / ".incoming"
    assert not incoming_dir.exists() or not any(incoming_dir.iterdir())


def test_http_upload_normal_with_admin_key(store):
    """Test normal HTTP upload with admin key."""

    def authorize(key: str | None = Header(default=None)):
        if key != "admin":
            raise HTTPException(403)

    app = FastAPI()
    app.include_router(router(lambda: store, authorize, resolve))
    client = TestClient(app)
    session = store.create_session()
    url = f"/sessions/{session}/attachments"

    # Normal upload
    response = client.post(
        url,
        headers={"key": "admin"},
        params={"name": "test.txt", "type": "text/plain"},
        content=b"normal content",
    )
    assert response.status_code == 200

    saved = response.json()
    assert saved["name"] == "test.txt"
    assert saved["size"] == len(b"normal content")

    # Check file was created
    file_path = attachment_files.blob_dir(store) / saved["id"]
    assert file_path.exists()
    assert file_path.read_bytes() == b"normal content"


def test_remove_purges_the_row_and_deletes_the_file(store):
    """Remove purges the row as before and, after its commit, deletes the disk file."""
    session = store.create_session()
    raw = b"content to remove"
    saved = upload_file(store, session, raw)
    file_path = attachment_files.blob_dir(store) / saved["id"]

    receipt = a.remove(store, session, saved["id"], resolve)
    assert receipt["reason"] == "owner-removed"

    # Row should be purged
    with store._connect() as db:
        row = db.execute("SELECT purged FROM attachments WHERE id=?", (saved["id"],)).fetchone()
        assert row[0] == 1

    assert not file_path.exists()

    # Redact should also purge rows only
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        a.redact(db, [session])
        db.commit()

    # Row should still be purged
    with store._connect() as db:
        row = db.execute("SELECT purged FROM attachments WHERE id=?", (saved["id"],)).fetchone()
        assert row[0] == 1
