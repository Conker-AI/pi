"""Removed and forgotten files leave the disk, not only the database."""

from __future__ import annotations

from contextlib import closing

from pi.content import attachment_files, attachments
from pi.conversation import settings as session_settings
from pi.conversation.forgetting import forget, preview
from pi.conversation.store import Store

SECRET = b"private-file-9d2a-" + "никому".encode()


def upload(store, session_id, data, name="notes.txt"):
    return attachments.upload(
        store,
        session_id,
        attachments.Metadata(name=name, type="text/plain"),
        data,
        resolve=session_settings.source_privacy,
    )


def test_removing_an_unsent_file_deletes_it_from_disk(tmp_path):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        item = upload(store, sid, SECRET)
        path = attachment_files.blob_dir(store) / item["id"]
        assert path.read_bytes() == SECRET
        attachments.remove(store, sid, item["id"], resolve=session_settings.source_privacy)
        assert not path.exists()


def test_forgetting_a_chat_deletes_its_files_and_keeps_other_chats_files(tmp_path):
    database = tmp_path / "pi.db"
    with closing(Store(database)) as store:
        root = store.create_session(title="root")
        child = store.create_session(parent_id=root, title="branch")
        other = store.create_session(title="other")
        forgotten = [upload(store, root, SECRET)["id"], upload(store, child, SECRET)["id"]]
        kept = upload(store, other, b"keep these notes")["id"]
        folder = attachment_files.blob_dir(store)
    forget(database, root, preview(database, root)["confirmation"])
    for identity in forgotten:
        assert not (folder / identity).exists()
    assert (folder / kept).read_bytes() == b"keep these notes"
    for file in folder.rglob("*"):
        if file.is_file():
            assert SECRET not in file.read_bytes(), file.name


def test_a_file_left_behind_by_a_crash_is_deleted_by_the_next_cleanup(tmp_path, monkeypatch):
    with closing(Store(tmp_path / "pi.db")) as store:
        sid = store.create_session()
        stale = upload(store, sid, SECRET)
        path = attachment_files.blob_dir(store) / stale["id"]
        # A removal that committed but died before deleting its file.
        sweep = attachment_files.delete_purged_files
        monkeypatch.setattr(attachment_files, "delete_purged_files", lambda db, folder: 0)
        attachments.remove(store, sid, stale["id"], resolve=session_settings.source_privacy)
        assert path.exists()
        monkeypatch.setattr(attachment_files, "delete_purged_files", sweep)
        fresh = upload(store, sid, b"another file")
        attachments.remove(store, sid, fresh["id"], resolve=session_settings.source_privacy)
        assert not path.exists()
        with store._connect() as db:
            assert attachment_files.delete_purged_files(db, attachment_files.blob_dir(store)) == 0
