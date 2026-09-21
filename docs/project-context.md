# Selected project context in actual turns

Session settings accept `projectId` and an optional `projectSources` list using
the same conversation/task/file references as the project's links. An empty list
imports no linked content. Up to 20 distinct, actually linked sources may be
selected. Selecting a project alone continues to apply only its instructions.

Submission reservation freezes source identities and the current message IDs,
not source text. Conversations include their current user/assistant text messages;
tasks include text messages from their associated runs. Files support extracted
plaintext and retain a content hash. Structured tool payloads, attached media in
referenced messages and implicit ancestor transcripts are not imported.

The frozen references are rendered as explicitly untrusted reference material in
the actual answer input, and participate in the ordinary context budget. Later
source messages are not added to a submitted turn. Limits are 200 messages per
source and 32000 serialized characters total; over-limit inputs fail explicitly,
without silent truncation or helper/model dispatch.

Each read checks authoritative source origin, availability and current/historical
privacy. Private target or source sessions cannot share project context. Archived
or forgotten sources fail closed. The selection confers no tool or memory grants.
The per-submission/per-turn settings snapshot exposes the manifest for inspection.
An explicit future settings change chooses a different source set; unlinking a
project entry does not rewrite an already submitted selection.

Pi records a content-free dependency from each source session to the consuming
session when reserving work. Read-only inspection does not record dependencies.
Offline forgetting previews and forgets dependent conversations as well as child
sessions, transitively, because their answers/summaries may contain derived source
content. This can include a consumer whose preparation failed: conservative
retention avoids claiming that a partially dispatched helper saw nothing.

Tests cover actual Loop inputs, stable message boundaries, explicit opt-in,
unlinked/private/oversized failures, files, inspection without mutation, and
physical forgetting of a source and its derived reply. Backend-only; no dashboard
transport or deployment changes.
