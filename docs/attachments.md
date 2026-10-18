# Durable conversation attachments

`pi.content.attachments` stores shared files on disk under the data directory (inside the Pi
data volume, so backups include it) with no size, count, or quota limits. The SQLite
database retains metadata, size, and SHA-256 for each attachment. No caller provides a
filesystem path; filenames are display metadata and cannot contain path separators or control
characters. Uploads do not invoke a model, fetch URLs, open documents, or execute
files. Downloads require an explicit authenticated owner request.

## Contract and storage

The source frontend contract is `conversation-attachments.ts`: IDs, names, sizes,
media types and last-modified timestamps. The backend assigns session-bound IDs,
computes actual byte size and SHA-256, and reports privacy, availability and
processing status separately. Client MIME declarations are never trusted as
permission to process a document.

Files are stored as `attachments/<attachment_id>` in the data directory. New uploads
are written to `attachments/.incoming/<uuid>` during upload, then moved to their final
location. The database stores the storage location (`storage='disk'` for new files,
`storage='database'` for legacy BLOB rows), real size, and SHA-256. Text extraction
is only performed for files up to 25 MiB; larger files are stored with processing
status `unsupported` and reason "Too large to read as text; stored as a file."

Bytes, metadata and privacy snapshots are immutable except for one-way content
purging. Both download and text extraction verify the retained bytes against
stored size and SHA-256. Missing files on disk cause integrity failures. Restarts
preserve the same bytes and IDs. Legacy BLOB rows (stored as `storage='database'`)
continue to work unchanged.

## Owner transport

The owner-only router factory accepts `(store_factory, owner_authorize, resolve)`:

- `POST /sessions/{session_id}/attachments?name=...&type=...&lastModified=...`
  streams request bytes to disk with no size limits. It does not parse multipart form data.
- `GET /sessions/{session_id}/attachments` lists resolved metadata.
- `GET /sessions/{session_id}/attachments/{id}` inspects one attachment.
- `GET /sessions/{session_id}/attachments/{id}/download` returns exact bytes as
  `application/octet-stream` via FileResponse for disk storage or Response for legacy BLOBs,
  with attachment disposition, `nosniff`, sandbox CSP, and `Cache-Control: no-store`;
  declared HTML/media MIME is never served inline.
- `GET /sessions/{session_id}/attachments/{id}/text` returns verified inert
  plaintext with `trust: untrusted-source`, or 415 when processing is unsupported.
- `DELETE /sessions/{session_id}/attachments/{id}` soft-purges an unbound,
  unreserved upload and returns an immutable identifier/time removal receipt.
  Repeating it returns the same receipt. Once the removal commits, the file is deleted from disk.

## Processing capabilities

Only declared `text/plain` is decoded, using strict UTF-8 (optional BOM), with a
200,000-character limit. Binary control characters are rejected for extraction.
Empty plaintext is supported. Files larger than 25 MiB are not read for text extraction
and are stored with processing `unsupported`. Oversized or invalid files are still retained
as bytes with an explicit unsupported-processing reason; they are never silently
truncated. Extraction is rechecked from verified bytes when requested.

HTML, Markdown MIME, JSON MIME, PDF, office documents, images, audio, video and
archives may be stored/downloaded but have **no processing capability** here.
There is no OCR, transcription, image understanding, document parsing, conversion,
code execution, malware-scanning claim, or automatic provider upload.

## Privacy and provenance

Every operation identifies both session and attachment. IDs from another session
return 404. Creation requires an active real session and authoritative internal
privacy via `session_settings.source_privacy`. Missing or malformed privacy fails
closed. Each upload retains its creation privacy, combined conservatively with
current session privacy: later settings relaxation cannot make a private file
public. Closed sessions remain owner-readable; forgotten sources do not.

`bind(db, session_id, user_message_id, attachment_ids)` requires the caller's active
transaction. It seals an immutable `message_attachment_sets` manifest (including
empty sets), then creates exact ordered `message_attachments`. Database triggers
reject cross-session/user-role violations and additions outside the sealed set.
It is an internal message-creation hook, not an endpoint for rewriting history.
`message_views` resolves metadata without exposing stored bytes or extracted text.

`attachment_reservations` holds a fixed `(request_id, attachment_id)` association
with foreign keys to submission receipts and attachments. Reservations are valid
only for an available attachment in that preparing submission's originating
session. Parent submission integration inserts them while reserving and deletes
them on bind/failure. Owner removal serializes against these rows and refuses an
attachment that has any reservation or message binding.

`project_source` exposes live metadata and conservative privacy only; it does not
copy file content into project records or authorize model access. Future context
assembly must retain private attachment flags when session privacy later relaxes.

## Integration and forgetting

Store initializes `attachments.initialize(db)` after submission schema, which installs
the schema and adds the `storage` column if missing. The Pi owner API mounts this router,
and project file resolution calls `project_source` with the authoritative privacy resolver.
No browser gateway allowlist or dashboard adapter is added.

Offline forgetting calls `attachments.redact(db, session_ids)` in its existing
transaction. After that commits, `attachments.delete_purged_files` deletes the disk file of
every purged attachment, then the existing checkpoint/vacuum cleanup runs. A crash in between
keeps the forgetting maintenance barrier, and the rerun deletes the files; any removal also
sweeps files left behind. Like any deleted file, a file's old disk blocks may remain until
the filesystem reuses them; the database pages are scrubbed by VACUUM.
The hook accepts old databases with no attachment table. Upload/removal never bypass the
Store maintenance lease.

The independent library does not yet feed uploaded text into a turn or implement
submission reservation lifecycle. Those hooks belong to the separate submission
increment; storing/extracting a file is not a claim that the model read it.

## Verification

Temporary-database tests cover restart/exact bytes, strict filename validation,
unsupported formats, private-source isolation, sealed bindings and limits,
competing quota writes, corrupt content rejection, inert HTTP download headers,
archive behavior, redaction, removal receipts/quota recovery and bound/reserved
removal conflicts. Parent integration tests exercise actual offline forgetting
and raw database/sidecar scans. The attachment and integration suites pass together
(26 tests); no private database or external service is used.
