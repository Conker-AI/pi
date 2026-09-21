# Owner terminal backend

`gateway.terminals.Terminals` manages ephemeral browser-session leases. Creation
requires validation and an authorization callback before spawning. A repeated request
ID returns the same terminal, including after closure; it never respawns it. Access
is bound to the originating browser session. A periodic sweep revalidates sessions
and closes revoked leases without client polling. Limits are one live terminal per
browser session, four globally and 1000 retained request identities per gateway
lifetime. Shutdown closes all leases. Two synthetic lease tests cover replay,
cross-session denial, revoked authorization and denied creation. HTTP routes and
the real AuthStore callbacks still need integration.

`pi.owner_terminal.Terminal` is an ephemeral Linux Bash PTY primitive. It is not
exposed through HTTP, tools or SystemGate. Authenticated owner admission, session
ownership and gateway expiry/revocation integration remain required before use.

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
