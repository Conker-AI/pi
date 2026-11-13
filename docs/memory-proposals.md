# Memory proposals (retired) and the correction capability

Memory proposals are retired (2026-11-12, [inbox-kinds.md](inbox-kinds.md) merge 2). A proposal
was an exact text change to one MemoryGate record at a known revision, which a caller holding
Pi's admin key filed and answered through `/memory-proposals` and which the Inbox listed as kind
`memory`. No Pi code, dashboard screen or CLI command ever created or answered one, so the routes
and the Inbox item are gone. The owner corrects memory himself through `/memory/corrections`, and
Conker's own "remember" is a [change request](change-requests.md).

The `memory_proposals` table stays (`pi/memory/proposals.py`): installs from before may hold rows
with a memory's old text, the proposed text and a reason. Nothing writes or lists them. Forgetting
the chat scrubs those fields and marks the row `forgotten`; so does forgetting the memory itself
(`POST /memory/forget`, see [memory-forget.md](memory-forget.md)).

## The correction capability

Memory corrections and forgetting a memory use a dedicated MemoryGate capability:

- `PI_MEMORY_CORRECTION_URL`: MemoryGate base URL.
- `PI_MEMORY_CORRECTION_KEY`: dedicated correction key, at least 32 characters.
- `PI_MEMORY_CORRECTION_AGENT_ID`: the fixed remote namespace.

MemoryGate uses matching `MEMORYGATE_CORRECTION_KEY` and
`MEMORYGATE_CORRECTION_AGENT_ID`. This is a dedicated correction capability, not
an admin credential, ingestion key or read key. Pi never forwards it to a browser
or a model. Missing configuration is explicit; partial configuration fails
startup. Nothing enables the capability or provisions secrets automatically.
