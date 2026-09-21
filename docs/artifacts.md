# Durable artifact library

`pi.artifacts` owns inert artifact records and immutable versions using Pi's existing
SQLite connection/maintenance boundary. It does not execute code, interpret HTML,
fetch media, write export files, invoke models, or send external requests.

The backend contract follows the native content and lifecycle semantics in
Companion's `artifact-types.ts` / `artifact-preview.ts`. It remains independent of
the frontend fixture client. Durable responses identify provenance as `pi`, not
`preview`; mutation bodies use `expected_revision` consistently with Pi's other APIs.
`execution` is always `not-wired`.

## Integration boundary

Store initialization executes `artifacts.SCHEMA`. The owner API includes
`artifacts_api.router(store_factory, owner_authorize, resolve)` with
`session_settings.source_privacy`; no gateway runtime allowlist entry is implied. The optional internal resolver has signature
`resolve(db, session_id)` and returns the authoritative boolean fields
`memoryDisabled`, `harnessDisabled`, and optional `incognito`. It must use the
provided transaction snapshot and must not accept client privacy assertions.
Absent, failing, or malformed resolvers fail closed for conversation copies.

`artifacts.redact(db, session_ids)` runs in the existing offline forgetting
transaction before its vacuum/checkpoint completion. It irreversibly clears the
source snapshot, title, and **all** versions (including owner edits/restores derived
from that source). Normal version UPDATE/REPLACE/DELETE operations are blocked by
SQLite triggers; deletion is allowed only for purged artifact records. The hook
accepts old databases without artifact tables. Purged records retain identifiers
and metadata, with empty version history; source copies can never revive.

## Owner routes

- `GET /artifacts` and `GET /artifacts/{id}` return privacy-resolved views.
- `POST /artifacts` creates owner content; source metadata is rejected.
- `POST /artifacts/from-message` copies an exact completed assistant final response.
- `POST /artifacts/{id}/versions` appends content with `expected_revision`.
- `POST /artifacts/{id}/restore` appends a historical version with `version` and
  `expected_revision`; history is never overwritten.
- `POST /artifacts/{id}/archive` uses `archived` and `expected_revision`.
- `GET /artifacts/{id}/export?version=N` returns an inert JSON export envelope.

Writes reserve SQLite's writer transaction before reading revision and allocating
version numbers. Stale writes return 409; unavailable records return 404; malformed
content returns 422. Bounds match the preview: 500 records, 100 versions per record,
250,000 serialized content characters, and 4,000,000 history characters.

Content is strictly typed and rejects unknown fields, nonfinite chart/diagram
numbers, mismatched rows, duplicate chart labels or node/edge identities, dangling
and self-referential edges, and media URLs with credentials or non-HTTPS schemes.
Media references remain references. HTML and code stay text. HTML/SVG/XML exports
use text file suffixes and plain-text MIME. CSV protects formula-leading cells,
including whitespace/control prefixes. Filenames cannot contain paths, control
characters, or Windows reserved device basenames.

## Live source checks and limits

Source identity and text are read from real `sessions`, `messages`, `turn_messages`,
and `turns` rows in the same transaction. Only a final assistant message associated
with a completed turn may be copied; legacy messages with no terminal provenance
are ineligible. Closed/forked source sessions are reported as `source-archived`:
read/export remains possible with known privacy, while edits are blocked. Missing,
forgotten, changed, incomplete, or privacy-unknown sources hide title and all version
bodies. Export and edit fail closed. Task references are resolved from real tasks,
checked against active originating sessions at creation, and rechecked on reads.

Pi's current transcript stores text, not the frontend's supplied citation records.
This increment therefore does not invent citations or claim citation retention;
`preserveCitations` is accepted for contract shape but has no effect until an
authoritative citation store is introduced. Privacy must come from an internal
session settings resolver; no session privacy is inferred from global configuration
or source text. This library does not enforce memory/harness privacy in execution.

No dashboard adapter, gateway wiring, deployment, external download, or execution
is included. Artifact persistence does not implement attachment storage, document
editing, project context selection, or broader deliverable generation.

## Evidence

`python -m pytest tests/test_artifacts.py -q` exercises temporary databases only:
restart persistence, database immutable history, competing revision writes,
restore/archive, strict schemas and size bounds, source privacy/provenance changes,
source archive/read/export, irreversible derived-content purge, task origins,
formula-safe CSV, inert HTML export, and authenticated route validation. The actual offline forgetting entry point is also exercised using authoritative
session settings, followed by Store reopen and raw database/sidecar byte inspection:
source text, private title, and derived versions disappear while unrelated
owner-authored artifacts survive. The artifact and forgetting suites pass together
(36 tests).
