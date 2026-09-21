"""Temporary attachment bytes, source isolation, bounds, HTTP and physical erasure."""

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pi import attachments as a
from pi import session_settings
from pi.attachments_api import router
from pi.store import Store

resolve = session_settings.source_privacy


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "attachments.db")
    yield value
    value.close()


def upload(store, session, raw=b"plain text", name="note.txt", media_type="text/plain"):
    return a.upload(
        store, session, a.Metadata(name=name, type=media_type, lastModified=123), raw, resolve
    )


def test_restart_content_integrity_and_plaintext(store):
    session = store.create_session()
    raw = "plain text \u05e9\u05dc\u05d5\u05dd".encode()
    saved = upload(store, session, raw)
    assert saved["size"] == len(raw)
    assert saved["sha256"] == hashlib.sha256(raw).hexdigest()
    assert saved["processing"]["status"] == "extracted"
    path = store.path
    store.close()
    reopened = Store(path)
    try:
        view, data = a.download(reopened, session, saved["id"], resolve)
        assert view == saved and data == raw
        assert a.extract(reopened, session, saved["id"], resolve)["text"] == raw.decode()
        assert a.listing(reopened, session, resolve) == [saved]
        with reopened._connect() as db:
            for sql in (
                "UPDATE attachments SET content=x'00'",
                "DELETE FROM attachments",
                "INSERT OR REPLACE INTO attachments SELECT * FROM attachments",
            ):
                with pytest.raises(sqlite3.IntegrityError):
                    db.execute(sql)
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "raw,media_type",
    [
        (b"<script>bad()</script>", "text/html"),
        (b"%PDF-1.0", "application/pdf"),
        (b"image", "image/png"),
        (b"audio", "audio/wav"),
        (b"PK\x00", "application/zip"),
        (b"binary\x00data", "text/plain"),
        (b"\xff\xff", "text/plain"),
        (b"x" * 200001, "text/plain"),
    ],
    ids=["html", "pdf", "image", "audio", "archive", "binary", "invalid-utf8", "large-text"],
)
def test_unsupported_processing_does_not_prevent_inert_storage(store, raw, media_type):
    session = store.create_session()
    saved = upload(store, session, raw, media_type=media_type)
    assert saved["processing"]["status"] == "unsupported"
    assert a.download(store, session, saved["id"], resolve)[1] == raw
    with pytest.raises(a.AttachmentError) as exc:
        a.extract(store, session, saved["id"], resolve)
    assert exc.value.status == 415


@pytest.mark.parametrize(
    "name", ["../secret", "C:\\secret", "sub/file", "..", "bad\r\nheader", "", "x" * 256]
)
def test_no_caller_filesystem_paths_or_header_injection(name):
    with pytest.raises(ValidationError):
        a.Metadata(name=name)


def test_session_and_privacy_isolation(store):
    session, other = store.create_session(), store.create_session()
    settings = session_settings.Update(
        expected_revision=0,
        settings=session_settings.Settings(
            agentId="companion",
            privacy=session_settings.Privacy(memoryDisabled=True, harnessDisabled=False),
        ),
    )
    session_settings.save(store, session, settings)
    saved = upload(store, session)
    assert saved["privateOrigin"] is True
    session_settings.save(
        store,
        session,
        settings.model_copy(
            update={
                "expected_revision": 1,
                "settings": session_settings.Settings(
                    agentId="companion",
                    privacy=session_settings.Privacy(memoryDisabled=False, harnessDisabled=False),
                ),
            }
        ),
    )
    assert a.get(store, session, saved["id"], resolve)["privateOrigin"] is True
    with pytest.raises(a.AttachmentError) as exc:
        a.download(store, other, saved["id"], resolve)
    assert exc.value.status == 404
    assert a.get(store, session, saved["id"])["name"] == "Unavailable attachment"
    with pytest.raises(a.AttachmentError):
        a.download(store, session, saved["id"])
    with store._connect() as db:
        assert (
            a.project_source(db, session, saved["id"], resolve)["privacy"]["memoryDisabled"] is True
        )
        assert a.project_source(db, other, saved["id"], resolve) is None


def test_binding_is_exact_bounded_and_immutable(store, monkeypatch):
    assert (a.MAX_FILES, a.MAX_BYTES, a.MAX_MESSAGE_BYTES) == (5, 10485760, 26214400)
    session, other = store.create_session(), store.create_session()
    first, second = upload(store, session, b"123456"), upload(store, session, b"abcdef")
    foreign = upload(store, other)
    message = store.append_message(session, "user", "question")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for ids in ([first["id"]] * 2, [str(i) for i in range(6)], [foreign["id"]]):
            with pytest.raises(a.AttachmentError):
                a.bind(db, session, message["id"], ids)
        monkeypatch.setattr(a, "MAX_MESSAGE_BYTES", 10)
        with pytest.raises(a.AttachmentError):
            a.bind(db, session, message["id"], [first["id"], second["id"]])
        a.bind(db, session, message["id"], [first["id"]])
        assert a.message_views(db, message["id"], resolve)[0]["id"] == first["id"]
        with pytest.raises(a.AttachmentError):
            a.bind(db, session, message["id"], [second["id"]])
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO message_attachments VALUES(?,?,?)", (message["id"], 1, second["id"])
            )
        db.commit()
    empty = store.append_message(session, "user", "none")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        a.bind(db, session, empty["id"], [])
        with pytest.raises(a.AttachmentError):
            a.bind(db, session, empty["id"], [first["id"]])
        db.commit()


def test_byte_limit_and_competing_session_quota(store, monkeypatch):
    session = store.create_session()
    with pytest.raises(a.AttachmentError) as exc:
        upload(store, session, b"x" * (a.MAX_BYTES + 1))
    assert exc.value.status == 413
    monkeypatch.setattr(a, "MAX_SESSION_BYTES", 10)
    barrier = Barrier(2)

    def save(_):
        barrier.wait()
        try:
            return upload(store, session, b"123456")["size"]
        except a.AttachmentError as exc:
            return exc.detail["code"]

    with ThreadPoolExecutor(2) as pool:
        assert sorted(map(str, pool.map(save, range(2)))) == ["6", "session_limit"]
    assert len(a.listing(store, session, resolve)) == 1


def test_corrupted_bytes_block_download_and_extraction(store):
    session = store.create_session()
    saved = upload(store, session)
    with store._connect() as db:
        # Simulate on-disk corruption beyond normal API/schema mutation guards.
        db.execute("DROP TRIGGER attachments_no_update")
        db.execute("UPDATE attachments SET content=x'616263' WHERE id=?", (saved["id"],))
    for operation in (a.download, a.extract):
        with pytest.raises(a.AttachmentError, match="integrity"):
            operation(store, session, saved["id"], resolve)


def test_owner_raw_http_upload_and_safe_download(store):
    def authorize(key: str | None = Header(default=None)):
        if key != "owner":
            raise HTTPException(403)

    app = FastAPI()
    app.include_router(router(lambda: store, authorize, resolve))
    client = TestClient(app)
    session = store.create_session()
    url = f"/sessions/{session}/attachments"
    assert client.post(url, params={"name": "x"}, content=b"x").status_code == 403
    response = client.post(
        url,
        headers={"key": "owner"},
        params={"name": "evil.html", "type": "text/html"},
        content=b"<script>bad()</script>",
    )
    assert response.status_code == 200
    identity = response.json()["id"]
    download = client.get(f"{url}/{identity}/download", headers={"key": "owner"})
    assert download.content == b"<script>bad()</script>"
    assert download.headers["content-type"] == "application/octet-stream"
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "no-store"
    assert client.get(f"{url}/{identity}/text", headers={"key": "owner"}).status_code == 415
    assert (
        client.post(
            url,
            headers={"key": "owner", "content-length": str(a.MAX_BYTES + 1)},
            params={"name": "huge"},
            content=b"",
        ).status_code
        == 413
    )


def test_archive_blocks_upload_but_allows_owner_read(store):
    session = store.create_session()
    saved = upload(store, session)
    store.close_session(session, "closed")
    assert a.get(store, session, saved["id"], resolve)["availability"] == "source-archived"
    assert a.download(store, session, saved["id"], resolve)[1] == b"plain text"
    with pytest.raises(a.AttachmentError):
        upload(store, session)


def test_redaction_erases_bytes_text_name_and_digest(store):
    session = store.create_session()
    saved = upload(store, session, b"private raw text", "private filename.txt")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        a.redact(db, [session])
        db.commit()
        row = db.execute(
            "SELECT name,content,extracted_text,sha256,privacy FROM attachments"
        ).fetchone()
        assert tuple(row) == ("", None, None, None, None)
    with pytest.raises(a.AttachmentError):
        a.download(store, session, saved["id"], resolve)


def test_unbound_removal_reclaims_quota_with_immutable_receipt(store, monkeypatch):
    session = store.create_session()
    monkeypatch.setattr(a, "MAX_SESSION_BYTES", 10)
    saved = upload(store, session, b"123456", "private-unbound.txt")
    with pytest.raises(a.AttachmentError):
        upload(store, session, b"123456")
    receipt = a.remove(store, session, saved["id"], resolve)
    assert receipt["reason"] == "owner-removed" and receipt["attachment_id"] == saved["id"]
    assert "private-unbound" not in str(receipt)
    assert a.remove(store, session, saved["id"], resolve) == receipt
    assert a.get(store, session, saved["id"], resolve)["availability"] == "removed"
    assert upload(store, session, b"123456")["size"] == 6
    with store._connect() as db:
        row = db.execute(
            "SELECT content,sha256,extracted_text,name FROM attachments WHERE id=?", (saved["id"],)
        ).fetchone()
        assert tuple(row) == (None, None, None, "")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE attachment_removals SET removed_at=0")
    with pytest.raises(a.AttachmentError):
        a.download(store, session, saved["id"], resolve)


def test_bound_or_reserved_upload_cannot_be_removed(store):
    from pi import submissions

    session = store.create_session()
    bound, reserved = upload(store, session), upload(store, session)
    message = store.append_message(session, "user", "attached")
    with store._connect() as db:
        db.execute("BEGIN IMMEDIATE")
        a.bind(db, session, message["id"], [bound["id"]])
        db.commit()
    with pytest.raises(a.AttachmentError, match="Bound or reserved"):
        a.remove(store, session, bound["id"], resolve)
    request_id = "attachment_reservation_8573"
    submissions.reserve(store, request_id, session, "pending", {})
    with store._connect() as db:
        db.execute("INSERT INTO attachment_reservations VALUES(?,?)", (request_id, reserved["id"]))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE attachment_reservations SET attachment_id=?", (bound["id"],))
    with pytest.raises(a.AttachmentError, match="Bound or reserved"):
        a.remove(store, session, reserved["id"], resolve)
    with store._connect() as db:
        db.execute("DELETE FROM attachment_reservations WHERE request_id=?", (request_id,))
    assert a.remove(store, session, reserved["id"], resolve)["reason"] == "owner-removed"
