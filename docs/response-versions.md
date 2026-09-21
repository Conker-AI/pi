# Response families

Completed alternative answers can share a response family. All immutable messages
remain in the transcript, with family/selection metadata. Context projects one
selected version at the original answer's chronological position. A branch captures
that exact selected message ID and is not changed by later parent selection.

Owner routes read a family and select a version with its expected revision:
`GET /sessions/{sid}/response-families/{root}` and
`POST /sessions/{sid}/response-families/{root}/select`.
Selection refuses foreign versions, unresolved work, stale revisions and context
rules referring to another version. Review those rules explicitly before switching.
Forgotten messages expose no family metadata and their families cannot be opened.

Retry completion uses this module to register and select its new answer atomically.
See response-retries.md for execution, cancellation and receipt behavior.

Validation: 26 combined version/context/fork/input tests passed; focused lint passed.
