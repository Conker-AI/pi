# Directory read receipts

Pi delegates directory metadata reads to ToolGate's fixed `system.files-list` tool. `Read` accepts a stable `request_id`, configured `root_id`, relative `path` (default empty), and `limit` (1–200, default 200). Paths reject empty components, dot traversal, backslashes, control characters and surrogates, matching ToolGate's grammar.

A request ID binds permanently to its root, path and limit. Concurrent submissions dispatch once; changed inputs conflict. Approvals retain the original request ID and resume explicitly with the saved approval. Unknown outcomes are inspected through the original ToolGate action receipt, never retried as fresh reads. Startup changes interrupted dispatches to unknown without invoking anything. Fresh observations require a new request ID.

Responses expose `listing`, its original `sampledAt`, and calculated `currentAgeSeconds`; they do not claim the saved receipt is current. Pi stores request metadata and approval references, not directory entries. ToolGate retains observations under its receipt policy. A later unavailable receipt does not erase a recorded completion, but returns no listing. Listing validation checks the exact requested root/path, timezone-aware sample time, entry count, unique safe child paths, recognized kinds and boolean truncation.

This is metadata browsing only. File content, writes, downloads, searches and terminal execution are separate capabilities. The module performs no filesystem access beyond Pi's own database and no direct SystemGate transport. Root authorization and Linux directory confinement remain ToolGate responsibilities. Tests use temporary Pi databases and synthetic ToolGate outcomes.
# Owner API and root discovery

`GET /system/files/roots` uses ToolGate's scoped root catalogue; no local filesystem
is accessed by Pi. Configured roots are distinct from observed directory results.
The transport validates metadata-only capabilities, safe root IDs/absolute paths
and uniqueness; it rejects redirects, compressed responses and oversized replies.
Unavailable configuration remains explicit. Responses use `Cache-Control: no-store`.
Its 64 KB response cap may reject larger configured catalogues; it never truncates
them into apparent success. A checked 10-second deadline can overrun during an
in-progress five-second network read.

`POST /system/files/listings`, `GET /system/files/listings/{request_id}` and
`POST /system/files/listings/{request_id}/resume` expose durable request, inspection
and saved-approval resume to the owner. Startup recovery marks unfinished requests
unknown without invoking ToolGate. Schema initialization is additive. No endpoint
reads file contents or writes files. Final frontend/gateway wiring remains deferred.
