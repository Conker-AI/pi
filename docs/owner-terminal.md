# Owner terminal backend

`gateway.terminals.Terminals` manages ephemeral browser-session leases. Creation
requires validation and an authorization callback before spawning. A repeated request
ID returns the same terminal, including after closure; it never respawns it. Access
is bound to the originating browser session. A periodic sweep revalidates sessions
and closes revoked leases without client polling. Limits are one live terminal per
browser session, four globally and 1000 retained request identities per gateway
lifetime. Shutdown closes all leases. Two synthetic lease tests cover replay,
cross-session denial, revoked authorization and denied creation. The routes below
integrate real AuthStore callbacks and exact password proofs.

Enable only through operator configuration: `GATEWAY_TERMINAL_SHELL=/bin/bash` and
`GATEWAY_TERMINAL_DIRECTORY=/an/explicit/workspace`. Default is disabled. The shell
runs on the gateway host/container with its OS permissions, not inside SystemGate.
POST `/api/terminal` with `{requestId}` requires an exact `/auth/verify` password
proof. This grants an interactive lease, not a proof for each keystroke. GET
`/api/terminal/{id}?cursor=0` returns base64 bytes, cursor and dropped-byte count.
POST suffix `/input` accepts `{data}` in base64; `/resize` accepts `{rows,columns}`;
`/close` accepts `{}`. All require the same authenticated browser session; writes
also require same-origin CSRF. No token is placed in a URL. Auth expiry/revocation
closes sessions through the background sweep; gateway shutdown closes all sessions.
No frontend terminal transport is connected yet.

The operator supplies an absolute shell path and directory. The child receives a
small explicit environment, no Pi/provider/gate credentials, no Bash startup files,
and disabled shell history. This is an owner shell running with Pi's OS privileges,
not a sandbox against reading accessible files or explicitly launching other tools.
No commands/output are persisted. Applications inside the shell may write files.

Input is bounded to 8192 bytes with a returned accepted-byte count; callers must not
automatically replay uncertain writes. Output uses a bounded byte buffer and cursor
with explicit dropped-byte reporting. Resize dimensions are bounded. An independent
expiry timer closes the PTY/process group even when no client polls; close is
idempotent. Detached processes are not a container isolation guarantee.

The stdlib `tests/owner_terminal_linux_check.py` ran under WSL Ubuntu Python 3.14
against a temporary directory and synthetic commands. It checked actual PTY I/O,
controlling-terminal job control, omitted synthetic credentials, close and expiry.
No owner files/commands, terminal API, frontend wiring or deployment were used.


## Combined Linux gateway verification

`python -m pytest tests/test_gateway_terminal_linux.py -q` runs the actual gateway
with a real Bash controlling PTY on Linux and temporary auth/shell storage. It
checks password-proof admission, CSRF, stable-request replay without another shell,
resize/output cursor/base64 transport, stripped inherited environment, and automatic
process closure after owner-session revocation. No listener, installed service,
ToolGate effect, or user file is used; HTTP requests stay inside TestClient.
The test skips on non-Linux systems. It passed under WSL with real password hashing.
Browser terminal rendering and deployment remain separate acceptance work.
