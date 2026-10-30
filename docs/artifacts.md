# Durable artifact library

`pi.content.artifacts` owns inert artifact records and immutable versions using Pi's existing
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
`session_settings.source_privacy`. Pi's browser contract narrowly exposes the JSON
owner routes listed below; the runtime credential cannot use them. The optional
internal resolver has signature
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

- `GET /artifacts?limit=50&cursor={artifact_id}` returns at most 100 compact,
  privacy-resolved summaries plus `nextCursor`; summaries never include version bodies.
- `GET /artifacts/{id}` returns the bounded immutable version history.
- `POST /artifacts` creates owner content; source metadata is rejected.
- `POST /artifacts/from-message` copies an exact completed assistant final response.
- `POST /artifacts/{id}/versions` appends content with `expected_revision`.
- `POST /artifacts/{id}/restore` appends a historical version with `version` and
  `expected_revision`; history is never overwritten.
- `POST /artifacts/{id}/archive` uses `archived` and `expected_revision`.
- `GET /artifacts/{id}/export?version=N` returns an inert JSON export envelope.

Every response is schema version `1`, identifies `provenance: "pi"` and
`authority: "none"`, and states whether content is included. Artifact views and
native exports also state `execution: "not-wired"`; no returned value grants tool,
model, provider, filesystem, or execution authority. IDs are canonical and bounded:
artifact IDs are `artifact_` plus 32 lowercase hex characters, session/message IDs
use 16 lowercase hex characters, and task IDs use 32 lowercase hex characters.
Unknown request fields and near-match browser paths fail closed.

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

Supplied assistant citations use `pi.conversation.citations`: strict IDs/labels/optional
HTTP(S) hrefs/excerpts, up to 100 entries and 100,000 serialized characters. They
are stored immutably beside exact final-message provenance, never inferred from
retrieved memory. Artifacts copy this exact evidence; Markdown edits preserve it
unless `preserveCitations` is false, non-Markdown edits drop it, and restore copies
the historical evidence. Source evidence changes hide stale artifact bodies.
Markdown export includes an explicitly unverified JSON appendix with escaped
backticks. Offline forgetting must redact message evidence and artifact history
in the same transaction. These metadata references are never fetched or executed.
`Completion.citations` validates supplied evidence; both final-response loop paths
pass it to `Store.complete_turn`, which commits citations atomically with message
and turn completion. Transcript reads return nonempty citations. OpenRouter's
actual `message.annotations` URL citations map to stable message-local IDs,
labels, links, and optional excerpts, following its
[documented response shape](https://openrouter.ai/docs/guides/features/plugins/web-search#parsing-web-search-results).
This adapter does not enable search/plugins or infer evidence from Markdown.
Providers without explicit annotations return no citations. Malformed metadata
fails the provider result; atomic persistence failure cannot leave a completed
turn with uncommitted evidence. Actual loop, Store rollback, provider mapping,
artifact propagation, forgetting, and raw-file erasure have regression coverage. Privacy must come from an internal
session settings resolver; no session privacy is inferred from global configuration
or source text. This library does not enforce memory/harness privacy in execution.

Downloads (`GET /artifacts/{id}/download` and the chat answer download below) are not in
the browser allowlist `OWNER_ROUTES`: the gateway's control relay forwards JSON only and
cannot carry a file, its type and its name. They are listed in `OWNER_DOWNLOADS`: the
owner-control key (the host CLI) and the recovery key fetch them straight from Pi, and the
browser through the gateway's file relay (`gateway/file_routes.py`, see
[browser-auth.md](browser-auth.md)), which checks the file's type, size and name on the way. No upload, arbitrary filesystem path,
deployment, external fetch, or execution is included.

## Evidence

`python -m pytest tests/test_artifacts.py tests/test_artifact_downloads.py
tests/test_citations.py tests/test_forgetting.py -q` exercises temporary databases only:
restart persistence, database immutable history, competing revision writes,
restore/archive, strict schemas and size bounds, source privacy/provenance changes,
source archive/read/export, irreversible derived-content purge, task origins,
formula-safe CSV, inert HTML export, and authenticated route validation. The actual offline forgetting entry point is also exercised using authoritative
session settings, followed by Store reopen and raw database/sidecar byte inspection:
source text, private title, and derived versions disappear while unrelated
owner-authored artifacts survive. Connected API coverage also verifies owner/runtime
credential separation, exact browser paths, compact cursor pagination, strict DTOs,
restart durability, stale-write conflicts, and owner-key downloads that stay outside the
JSON relay.


## Downloadable deliverables

`GET /artifacts/{id}/download?version=N&format=native|docx|pdf|pptx|xlsx` performs the same
live source/provenance checks as export and returns an attachment with no-store and nosniff
headers. `GET /sessions/{session_id}/messages/{message_id}/download?format=docx|pdf|pptx|xlsx`
does the same for one completed final answer, with the checks a copy into an artifact makes:
an unknown or forgotten answer is 404 `not_found`, anything but a completed final answer is
422 `not_an_answer`, and unknown chat privacy is 409 `privacy_unknown`. Both are owner-only.
Native formats match the JSON export contract (formula-protected CSV, inert HTML text).

| Content | DOCX | PDF | PPTX | XLSX |
|---|---|---|---|---|
| Markdown (artifact or answer) | yes | yes | yes | its tables, one sheet each |
| Table | yes | yes | yes | yes |
| Code | yes | yes | yes | no |

Markdown is read the way the chat shows it: CommonMark with GitHub tables, strikethrough
and task lists, a single line break as a space, raw HTML as literal text. Headings stay
headings (Word heading styles, PDF heading sizes, a new slide at level 1 and 2), lists keep
their numbers, bullets, task boxes and nesting as indented paragraphs, quotes are indented,
code keeps its lines in a monospaced block, rules become a border (a new slide in PPTX), and
tables keep their header, cells and column alignment. Only http, https and mailto links
without credentials stay clickable; any other link (javascript:, data:, file:, a citation
id) stays its plain label. Images are never fetched: they read "Image: alt" and, when the
address is safe, an "Open image" link. Supplied citations become a "Supplied source
references" list marked as not independently verified. XLSX cells are always text, so
formula-looking cells, leading zeros and markup stay literal; a sheet is named by the
heading above its table. No export contains formulas, macros, scripts or embedded images.

Files are made in a short-lived worker process (`pi/content/_export_worker.py`, isolated
Python, at most two at once, 40 s, and on Linux 30 s CPU and 1 GiB memory) that receives the
content on stdin and returns the file on stdout; nothing is written to disk. A refusal is
422 with a fixed code and a plain message: `export_too_long` (400,000 characters),
`export_too_deep` (lists and quotes more than 12 levels inside each other; never silently
truncated), `export_too_many_blocks` (10,000), `export_table_too_big` (64 columns, 2,000 rows,
200 tables, 40,000 cells, 32,000 characters a cell), `export_page_cells` (tables over 10,000
cells export only as XLSX), `export_pages` (500 PDF pages), `export_slides` (300 slides),
`export_too_big` (16 MB), `export_layout` (a cell or block taller than a PDF page),
`export_no_table` and `export_control` (characters XML cannot hold). A worker that is busy,
overruns or crashes is 503 `export_busy` or `export_unavailable`.

File names keep letters, digits, `-` and `_` only and avoid Windows device names. The
plain `filename` is ASCII (`artifact` when nothing is left), and `filename*` carries the
readable UTF-8 name, so a Cyrillic title keeps its name in browsers that read it.

PDF text uses DejaVu (Debian's `fonts-dejavu-core`, installed in the image) for Latin,
Cyrillic and Greek letters; without it, ReportLab's bundled Vera fonts cover Latin only and
other letters draw empty. DejaVu has Hebrew and Arabic glyphs, but right-to-left lines are
neither reordered nor shaped, and CJK has no font yet: DOCX keeps those scripts correctly.

Libraries, all pinned in `requirements.txt` and checked by the weekly pip-audit:
[python-docx 1.2.0](https://pypi.org/project/python-docx/1.2.0/) (MIT),
[openpyxl 3.1.5](https://pypi.org/project/openpyxl/3.1.5/) (MIT),
[python-pptx 1.0.2](https://pypi.org/project/python-pptx/1.0.2/) (MIT, brings XlsxWriter,
BSD-2-Clause), [markdown-it-py 4.2.0](https://pypi.org/project/markdown-it-py/4.2.0/) (MIT,
brings mdurl, MIT) and [ReportLab 5.1.0](https://pypi.org/project/reportlab/5.1.0/)
(BSD-3-Clause, pure Python, brings charset-normalizer, MIT). Tests reopen every file with
its reader library (python-docx, pypdf, python-pptx, openpyxl) and check text, cells, links,
names, limits and refusals: `tests/test_markdown_blocks.py`, `tests/test_deliverable_exports.py`,
`tests/test_artifact_downloads.py`.
