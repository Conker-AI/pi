"""Attachment bytes on disk: where they live, how they are written, checked and deleted."""

import contextlib
import hashlib
import os
import uuid
from pathlib import Path


def blob_dir(store):
    """Where attachment files live: next to the database, inside Pi's data volume."""
    return Path(store.path).parent / "attachments"


def db_blob_dir(db):
    """The same folder, found from an open connection (its main database file)."""
    return Path(db.execute("PRAGMA database_list").fetchone()[2]).parent / "attachments"


def file_digest(path, chunk=1024 * 1024):
    """SHA-256 and size of a file, read in chunks so a large file never sits in memory."""
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as stream:
        while block := stream.read(chunk):
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()


class Incoming:
    """Writer for incoming attachment files with SHA-256 and size tracking."""

    def __init__(self, store):
        self.store = store
        self.dir = blob_dir(store) / ".incoming"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / uuid.uuid4().hex
        self.file = open(self.path, "wb")  # noqa: SIM115
        self.sha256 = hashlib.sha256()
        self.size = 0

    def write(self, chunk):
        """Write a chunk and update hash and size."""
        self.file.write(chunk)
        self.sha256.update(chunk)
        self.size += len(chunk)

    def close(self):
        """Finalize the file and return path, size, and SHA-256."""
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()
        return self.path, self.size, self.sha256.hexdigest()

    def discard(self):
        """Delete the temporary file."""
        with contextlib.suppress(Exception):
            self.file.close()
        with contextlib.suppress(Exception):
            self.path.unlink()


def delete_purged_files(db, folder):
    """Delete the disk file of every purged (removed or forgotten) attachment; safe to repeat.

    Runs after the purge commits. A crash in between leaves the row purged and the file in
    place; the next removal or forgetting run deletes it.
    """
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='attachments'").fetchone():
        return 0
    if "storage" not in {row[1] for row in db.execute("PRAGMA table_info(attachments)")}:
        return 0
    deleted = 0
    for (identity,) in db.execute(
        "SELECT id FROM attachments WHERE storage='disk' AND purged=1"
    ).fetchall():
        try:
            (Path(folder) / identity).unlink()
            deleted += 1
        except FileNotFoundError:
            pass
    return deleted
