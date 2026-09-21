# Context controls: first runtime increment

Owner-only `/context/{session_id}` GET/POST saves strict, revisioned session instructions, per-message policies and an estimated budget. Initial expected revision is zero. Foreign-message selectors are rejected. `/context/{session_id}/turns/{turn_id}` reads the policy captured atomically when that turn was created; changes never replace an earlier turn's policy.

The model loop now inserts session instructions separately from transcript history and omits explicitly excluded messages. Keep-exact and allow-summary both retain exact content in this increment. Retrieve blocks dispatch until an authorized retrieval selection is available. Token accounting uses UTF-8 bytes divided by four plus framing, not a provider tokenizer or upper bound; provider limits remain authoritative. Overflow never silently drops pins.

Existing unconfigured conversations retain their prior behavior. Explicit policies currently block automatic and explicit summary forks rather than losing instructions/pins; reviewed fork policy transfer is still required. Existing parent summaries remain untrusted summary context and are not yet individually editable here. Model-role summarization, retrieval selection, inherited agent/project layers and effective input snapshots remain open requirements.

Offline forgetting clears both current instructions and per-turn policy payloads inside the existing exclusive redaction transaction, followed by the existing WAL/vacuum cleanup. The browser gateway allowlist and frontend are unchanged.

Validation: actual loop recorder observes excluded content absent and exact pins/instructions present; selectors/revisions/retrieval/overflow prevent invalid dispatch; policy snapshots survive later edits; raw temporary database inspection confirms forgotten instruction text is removed. Context, loop, submissions and forgetting suites: 54 passed.
