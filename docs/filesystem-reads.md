# Directory read receipts

Pi delegates directory metadata reads to ToolGate's fixed `system.files-list` tool. `Read` accepts a stable `request_id`, configured `root_id`, relative `path` (default empty), and `limit` (1–200, default 200). Paths reject empty components, dot traversal, backslashes, control characters and surrogates, matching ToolGate's grammar.

A request ID binds permanently to its root, path and limit. Concurrent submissions dispatch once; changed inputs conflict. Approvals retain the original request ID and resume explicitly with the saved approval. Unknown outcomes are inspected through the original ToolGate action receipt, never retried as fresh reads. Startup changes interrupted dispatches to unknown without invoking anything. Fresh observations require a new request ID.

Responses expose `listing`, its original `sampledAt`, and calculated `currentAgeSeconds`; they do not claim the saved receipt is current. Pi stores request metadata and approval references, not directory entries. ToolGate retains observations under its receipt policy. A later unavailable receipt does not erase a recorded completion, but returns no listing. Listing validation checks the exact requested root/path, timezone-aware sample time, entry count, unique safe child paths, recognized kinds and boolean truncation.

This is metadata browsing only. File content, writes, downloads, searches and terminal execution are separate capabilities. The module performs no filesystem access beyond Pi's own database and no direct SystemGate transport. Root authorization and Linux directory confinement remain ToolGate responsibilities. Tests use temporary Pi databases and synthetic ToolGate outcomes.
