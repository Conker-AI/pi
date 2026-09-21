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
