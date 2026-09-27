# Owner terminal sidecar candidate

> **Not a release deployment.** Pi source contains a candidate networkless sidecar
> supervisor and a Gateway Unix-socket client, but release manifests do not run or
> configure them. A minimal dedicated image and local health command now exist and
> pass a Docker Linux protocol drill. A published release digest, recovery policy and
> target-Ubuntu confinement acceptance are still missing. The capability remains
> deferred.

`gateway.terminals.Terminals` manages ephemeral browser-session leases. Creation
requires validation and an authorization callback before spawning. A repeated request
ID returns the same browser lease, including after closure; it never respawns it. The
sidecar independently retains the same bounded creation identity. Access
is bound to the originating browser session. A periodic sweep revalidates sessions
and closes revoked leases without client polling. Limits are one live terminal per
browser session, one globally and 1000 retained request identities per gateway
lifetime. Shutdown closes all leases. Two synthetic lease tests cover replay,
cross-session denial, revoked authorization and denied creation. The routes below
integrate real AuthStore callbacks and exact password proofs.

Gateway connects only when both `GATEWAY_TERMINAL_SOCKET` and
`GATEWAY_TERMINAL_WORKSPACE_LABEL` are configured. Default is disabled and release
manifests intentionally omit both. The separate `pi.terminal_sidecar` process owns
the allowlisted shell and fixed workspace. It admits one gateway connection after
exact Linux `SO_PEERCRED` UID validation, unlinks the listener, never reconnects and
closes every PTY when the channel is lost.
POST `/api/terminal` with `{requestId}` requires an exact `/auth/verify` password
proof. This grants an interactive lease, not a proof for each keystroke. GET
`/api/terminal/{id}?cursor=0` returns base64 bytes, cursor and dropped-byte count.
POST suffix `/input` accepts `{data}` in base64; `/resize` accepts `{rows,columns}`;
`/close` accepts `{}`. All require the same authenticated browser session; writes
also require same-origin CSRF. No token is placed in a URL. Auth expiry/revocation
closes sessions through the background sweep; gateway shutdown closes all sessions.
The Conker dashboard now connects these exact routes through a strict typed client
and xterm workspace. It recovers the current browser lease, does not persist content,
and stops accepting input after uncertain delivery until the owner explicitly resumes
without replay. Its local preview is synthetic and executes no host commands.

The sidecar operator supplies an absolute image-local shell and `/workspace`. The
child receives a small explicit environment, no Pi/provider/gate credentials, no
Bash startup files and disabled shell history. Isolation still depends on the
unshipped container contract: non-root UID distinct from Gateway, no network, no
credentials or auth mount, read-only root and one workspace mount. No commands or
output are persisted. Applications inside the shell may write workspace files.

Input is bounded to 8192 bytes with a returned accepted-byte count; callers must not
automatically replay uncertain writes. Output uses a bounded byte buffer and cursor
with explicit dropped-byte reporting. Resize dimensions are bounded. An independent
expiry timer closes the PTY process group even when no client polls. The isolated
sidecar then sweeps every other process running under its dedicated container UID,
except PID 1 and the supervisor itself. This second pass removes descendants that
escaped the original process group. If the sweep cannot prove that no peer remains,
the sidecar becomes permanently unready and must be recreated. The one-live-lease
limit is what makes this UID-wide cleanup unambiguous. Close is idempotent.

The stdlib `tests/owner_terminal_linux_check.py` previously ran under WSL Ubuntu Python 3.14
against a temporary directory and synthetic commands. It checked actual PTY I/O,
controlling-terminal job control, omitted synthetic credentials, close and expiry.
No owner files/commands, sidecar channel, frontend wiring or deployment were used.


## Combined Linux gateway verification

`python -m pytest tests/test_gateway_terminal_linux.py -q` runs the actual gateway
with a real Bash controlling PTY on Linux and temporary auth/shell storage. It
checks password-proof admission, CSRF, stable-request replay without another shell,
resize/output cursor/base64 transport, stripped inherited environment, and automatic
process closure after owner-session revocation. No listener, installed service,
ToolGate effect, or user file is used; HTTP requests stay inside TestClient.
The test skips on non-Linux systems. It passed under WSL with real password hashing.
Publishing the dedicated image and accepting a real deployment remain separate work.
Browser rendering and a local Docker sidecar drill are verified, but neither the
earlier WSL result nor Docker Desktop satisfies ADR-0012's target-Ubuntu gate.

`sh scripts/terminal_container_acceptance.sh IMAGE` is the repeatable local and
target-host container drill. It launches the exact image twice with the hardened
runtime flags. The normal pass checks PTY I/O, resize, stripped environment, absent
credential/Docker mounts, no network route and escaped-descendant cleanup. The hostile
pass creates a replacement control listener from inside the PTY, terminates the real
supervisor and proves the Gateway channel closes without reconnecting to the fake
listener. It emits one secret-free JSON result with the tested image and local image
ID. A local result remains pre-promotion evidence; target acceptance must use the
published digest and retain the result with the generated manifest and browser tests.
