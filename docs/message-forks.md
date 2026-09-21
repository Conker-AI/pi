# Per-message branches

Owner POST /sessions/{id}/messages/{message_id}/fork accepts a retained request_id,
expected_settings_revision, expected_context_revision and optional reviewed_summary.
It creates an open child through the selected user input/final response, leaving
the original conversation open. A replay returns the same child; conflicting
request reuse fails. Fork creation invokes no provider or tool.

The child references original message IDs using the existing inherited-context
store. Messages and memory-ingest events are not duplicated. Existing instructions,
project and agent settings carry forward; context policy entries are restricted
to the prefix. Exact pins after the boundary require review instead of silent
removal. If the parent has a summary, reviewed_summary is required because that
summary may describe content after the chosen boundary. Empty reviewed_summary
explicitly discards it. No automatic summary is generated.

The child keeps the strictest privacy modes of its inherited source messages.
Those modes cannot be relaxed while it contains those references. Forgotten or
unknown-privacy sources block creation. Session forgetting includes descendants
through existing parent lineage. Historical tool results are evidence of actions
that already happened; creating a branch does not undo or repeat those actions.

GET /sessions/{id}/branch-history exposes inherited plus local messages in order,
with inherited flags and bounded pages (limit 1-200, message-ID cursor). Source
identity remains the original message's session/id. Creation currently requires
resolved ordinary-conversation work; calls and team-role sessions use their own
controls. A mid-tool intermediate output is not a valid fork boundary.
Per-message retry with another model is still a separate pending operation.
