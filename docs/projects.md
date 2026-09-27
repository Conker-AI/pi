# Durable owner projects

`/projects` has a schema-versioned owner-control contract, exposed by the browser Gateway under
`/api/control/pi/projects` after owner login. The exact owner surface is:

- `GET /projects?limit=50&cursor=project_...` lists at most 200 projects per page.
- `POST /projects` creates bounded name, description and instruction fields.
- `GET /projects/{id}` reads one project.
- `POST /projects/{id}/update` applies `{expected_revision, fields}`.
- `POST /projects/{id}/archive` applies `{expected_revision, archived}`; `false` restores.
- `POST /projects/{id}/link` and `/unlink` apply `{expected_revision, reference}`.

All writes require the Gateway's operation-bound password verification and Pi's distinct owner
credential. Runtime credentials are denied. Project paths accept only server-generated
`project_` IDs containing 32 lowercase hexadecimal characters. Responses carry
`schemaVersion: 1`, bounded fields and links, and explicit `authority: none`,
`contentIncluded: false`, and `grantsInherited: false` markers. Mutations use durable optimistic
revisions; stale changes return 409 and concurrent writers serialize in SQLite.

References retain identities and link-time origin only. Conversation, task and file references
must use Pi's canonical `ses_`, `tsk_`, and `attachment_` identities. Current labels/privacy come
from Pi's internal authorized resolver; missing, forgotten, malformed or moved sources never
expose cached labels. Link responses contain metadata and privacy state, never transcript or file
content. Source selection grants no permissions, provider authorization, memory writes or
execution authority.

Actual context association remains the existing owner-only
`POST /sessions/{session_id}/settings` contract: it selects one active `projectId` and at most 20
distinct linked `projectSources`, then runtime capture rechecks project state, source origin,
privacy and size. The project API does not accept caller-provided labels, privacy, filesystem paths
or source contents.

Project removal, metadata search and the legacy caller-supplied privacy preview remain
recovery-admin only and are intentionally absent from the browser allowlist. Removal can erase an
empty archived project, so the owner UI preserves archived records as tombstones. The preview
cannot authoritatively represent a target conversation's privacy; the session-settings path does.

Tests use temporary SQLite files and cover owner/runtime auth separation, restart persistence,
pagination, competing updates, stale revisions, archive/restore and removal safeguards, live
labels, private/changed origins, canonical IDs, strict schemas and exact browser paths.
