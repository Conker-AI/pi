# Per-message narration retry

Owner POST `/sessions/{sid}/messages/{mid}/retry` accepts `request_id` (16-128
letters/digits/underscore/hyphen) and an eligible catalogue `model_id`.
GET `/sessions/{sid}/response-retries/{request_id}` inspects the retained receipt.

Admission atomically creates a new turn and freezes the selected catalogue route.
Only the caller that created the receipt calls the provider. Duplicate IDs inspect
that same attempt, including failures, cancellation and process interruption. A
new attempt requires a new request ID. Original and later messages remain intact.
The receipt retains original input identity without copying the user message.

The answer uses saved instructions, source IDs, policy, recalled evidence, reply
marker and attachment passages. It excludes the original answer and later messages.
Missing snapshots, unavailable references, unresolved source actions and stricter
current privacy fail closed. Calls and team sessions retain their separate controls.

This path generates narration only. It does not dispatch tools, rerun retrieval,
repeat original effects or automatically fall back from the selected model. It
retains original attachment citation identities. Successful completion atomically
records cost/turn outcome, the final message and response-family membership.

The new version becomes selected unless an exact context rule would be silently
dropped; in that case it remains inspectable while the previous selection stays.
Selection can then be changed after explicit context review. The conversation queue
is paused for retry and requires explicit resumption afterward. Stop uses the same
owner turn-cancellation endpoint and final-persistence guard as ordinary turns.

No frontend transport has been connected and no live provider calls were made.
