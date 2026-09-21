# Durable conversation attachments

`pi.attachments` stores bounded bytes in Pi's SQLite database, using the existing
Store transaction and maintenance lease. No caller provides a filesystem path;
filenames are display metadata and cannot contain path separators or control
characters. Uploads do not invoke a model, fetch URLs, open documents, or execute
files. Downloads require an explicit authenticated owner request.

## Contract and limits

The source frontend contract is `conversation-attachments.ts`: IDs, names, sizes,
media types and last-modified timestamps. The backend assigns session-bound IDs,
computes actual byte size and SHA-256, and reports privacy, availability and
processing status separately. Client MIME declarations are never trusted as
permission to process a document.

Limits are 10 MiB per file, five distinct files and 25 MiB per message. Each session
also has an independent storage quota of 100 active uploads and 100 MiB. Upload
limits are enforced while reading HTTP chunks and again before the transaction;
quota reservations serialize through `BEGIN IMMEDIATE`. File/message binding
limits are checked inside the caller's message transaction.

Bytes, metadata and privacy snapshots are immutable except for one-way content
purging. Both download and text extraction verify the retained bytes against
stored size and SHA-256. Restarts preserve the same bytes and IDs.

## Owner transport

The owner-only router factory accepts `(store_factory, owner_authorize, resolve)`:

- `POST /sessions/{session_id}/attachments?name=...&type=...&lastModified=...`
  accepts raw request bytes. It does not parse multipart form data.
- `GET /sessions/{session_id}/attachments` lists resolved metadata.
- `GET /sessions/{session_id}/attachments/{id}` inspects one attachment.
- `GET /sessions/{session_id}/attachments/{id}/download` returns exact bytes as
  `application/octet-stream`, attachment disposition, `nosniff`, sandbox CSP,
  and `Cache-Control: no-store`; declared HTML/media MIME is never served inline.
- `GET /sessions/{session_id}/attachments/{id}/text` returns verified inert
  plaintext with `trust: untrusted-source`, or 415 when processing is unsupported.
- `DELETE /sessions/{session_id}/attachments/{id}` soft-purges an unbound,
  unreserved upload and returns an immutable identifier/time removal receipt.
  Repeating it returns the same receipt. Active quota is reclaimed. Bound or
  reserved files require session forgetting. Soft removal clears logical content;
  it does not claim physical erasure of old SQLite pages.

## Processing capabilities

Only declared `text/plain` is decoded, using strict UTF-8 (optional BOM), with a
200,000-character limit. Binary control characters are rejected for extraction.
Empty plaintext is supported. Oversized or invalid plaintext is still retained
as bytes with an explicit unsupported-processing reason; it is never silently
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

Store initializes `attachments.SCHEMA` after submission schema. The Pi owner API
mounts this router, and project file resolution calls `project_source` with the
authoritative privacy resolver. No browser gateway allowlist or dashboard adapter
is added.

Offline forgetting calls `attachments.redact(db, session_ids)` in its existing
transaction, then performs its existing checkpoint/vacuum cleanup. This clears
bytes, extracted text, filenames, MIME metadata, hashes and privacy snapshots;
only content-free identities/provenance and removal receipts remain. The hook
accepts old databases with no attachment table. Upload/removal never bypass the
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
