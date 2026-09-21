# Reviewed summary versions

Owner context routes expose GET/POST `/context/{session}/summary` and POST
`/context/{session}/summary/restore`. Edits require the current revision. The first
edit retains the existing summary as revision zero; restoring creates a new version
instead of overwriting history. Reads return the latest 100 saved review versions.

The session's summary is the input already consumed by Pi's existing history
assembly. It stays labeled untrusted context, not permission or instruction.
Edits are rejected while a submission is preparing or work is unresolved. Original
messages and exact-pin selections are never rewritten. Each saved version retains
its parent-session source reference; no claim of sentence-level provenance is made.

Offline forgetting removes the affected summary history and scrubs physical copies.
These are owner-review versions; automatic summary generation remains the existing
fork pipeline. Provider/helper evaluations and frontend wiring are separate work.
