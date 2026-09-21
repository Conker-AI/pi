# Prepared answer context

Pi records each running turn's last prepared answer input as ordered message IDs
and non-message prompt segments. This preserves the instruction, summary and
recalled-context boundary needed by a later narration retry. Completed turn
snapshots cannot be changed by later conversation reads. Forgetting erases the
saved prompt segments in the same transaction as the source conversation.

This is internal retry groundwork, not a retry endpoint or permission to repeat
an action. Older turns without a snapshot remain explicitly unavailable. Source
message references must be revalidated before reuse; deleted or forgotten inputs
must never be reconstructed from another conversation.

Verification: 49 focused context, submission and forgetting tests pass.

The internal replay reader reconstructs that exact input, including reply markers
and attachment passage IDs, without retrieval, model calls or tool dispatch. It
revalidates project sources and refuses forgotten inputs, unresolved actions,
legacy/missing snapshots, call/team turns, and newly tightened privacy. It retains
the original context policy even if the current conversation policy has changed.
It is not an execution authorization: retry admission still needs an atomic claim,
current permission checks and a retained request identity.

Verification: 31 combined prepared-context, context-control, reply-target,
attachment and message-fork tests pass. Retry execution and response-family selection are documented separately in
response-retries.md and response-versions.md.
