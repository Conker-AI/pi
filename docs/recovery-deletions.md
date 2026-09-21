# Replay Pi deletions into a held recovery

Stop both Pi instances. Preserve the current authoritative database separately
from the restored copy. With matching current schemas, inspect the replay scope:

```sh
python -m pi.recovery_deletions preview --source /current/pi.db --target /recovery/pi.db
python -m pi.recovery_deletions replay --source /current/pi.db --target /recovery/pi.db --confirm HASH
```

The hash binds the restored session IDs and source tombstones. Replay verifies
database integrity and that the source covers restored session identities, holds
exclusive process leases on both stores, and uses the existing forgetting path
for messages, descendant sessions, artifacts, attachments, queued work, calls,
context and MemoryGate deletion outbox records. It scrubs free pages and WAL.
Changing deletion scope invalidates the preview. Partial failure retains a
startup hold; replay is idempotent and retries unfinished physical scrubbing.

The supplied source must be the surviving authoritative database. Identity checks
do not prove it is the latest copy. Missing/unavailable evidence cannot establish
that no later deletion occurred. This tool replays Pi session forgetting only;
it does not cover independent MemoryGate record deletion or character deletion.

**The restored copy remains held even on success.** Store refuses to open it while
the recovery marker table exists. No promotion or hold-removal command is provided:
MemoryGate tombstones/indexes, newer effect receipts and credential revocations
still require coordinated reconciliation. No provider or external action runs.
The source is read-only throughout. No content appears in the plan or receipt.
