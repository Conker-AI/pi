# Durable owner projects

`/projects` owner administration routes support create/list/get, revisioned update/archive/restore/remove, link/unlink, metadata search and context selection. Mutations require `expected_revision`; stale changes return 409. Only empty archived projects can be removed, and source records are never deleted with them.

References retain identities and link-time origin only. Current labels/privacy come from an internal authorized resolver; missing or moved sources never expose cached labels. Context selection excludes archived/private/unavailable sources and grants no permissions, provider authorization or memory write access. A project instruction is a separate owner instruction layer. The selection result does not retrieve or dispatch content.

The default resolver is absent and fails closed: durable project CRUD works, but linking is unavailable until Pi session privacy and conversation file metadata can be resolved authoritatively. Do not substitute caller-provided labels/privacy or filesystem paths. This is a backend integration requirement still open, not a completed source-link runtime. Dashboard gateway wiring remains deferred.

Tests use temporary SQLite files and cover restart persistence, competing updates, archive/removal safeguards, live labels, private/changed origins, strict schemas and guarded API behavior.
