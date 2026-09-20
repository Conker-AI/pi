# Browser authentication (B4)

Backend implemented on `feat/browser-auth`; release and ToolGate integration gates
are listed below. Dashboard screens remain a separate product build; the gateway
can optionally serve that build at its existing HTTPS origin.

The gateway is a separate process and data volume, shipped in the Pi image.
It owns a single owner's password and server-side sessions. Pi receives only a
hash of the runtime credential; it never receives the gateway's ToolGate owner credential,
password hash, session database or TLS private key. No service credential reaches
the browser. Only explicitly listed runtime operations may be forwarded.

The cookie is Secure, HttpOnly, SameSite=Strict and scoped to `/`. Unsafe requests
require an exact allowed Origin and a session-bound CSRF token, including login.
Successful login rotates the session. Logout, expiry, password reset and explicit
revocation invalidate server-side sessions. Password recovery requires access to
the host, never an email link or a service execution credential.

ToolGate integration contract requested from its owner: `X-ToolGate-Owner-Key`
on `GET /v2/owner/requests` and `POST /v2/owner/requests/{id}/decision`.
No fallback to ToolGate's admin or execution channel is permitted.

Recovery restores the password verifier, but invalidates every restored session.
A host password reset grants a new login; it cannot reconstruct lost transcripts,
vault keys or deleted memories, and cannot recall an action already dispatched.

## Backend contract

| Operation | Request / result |
| --- | --- |
| `GET /auth/session` | Creates an anonymous cookie if necessary; returns `authenticated`, `csrf_token`, `session_id`, absolute `expires_at`, `setup_required`. |
| `POST /auth/login` | JSON `{"password":"..."}`; rotates the cookie and CSRF token on success. |
| `POST /auth/logout` | Revokes the current session and clears its cookie. |
| `GET /auth/sessions` | Lists session IDs and timestamps, never bearer tokens. |
| `POST /auth/sessions/{id}/revoke` | Revokes one session. |
| `POST /auth/revoke-all` | Revokes every session, including the caller's. |
| `/api/pi/{path}` | Only methods and paths in `pi/browser_contract.py`; forwards using `X-Pi-Gateway-Key`. |
| `GET /api/owner/requests` | Reads the approval list through the dedicated ToolGate owner channel. |
| `POST /api/owner/requests/{id}/decision` | JSON `status` (`approved`, `rejected`, `dismissed`) and optional `note`; uses only the owner channel. |
| `GET /health` | Reports login setup and dependency health without returning conversation or approval content. |

For every unsafe request, including login, send the exact configured `Origin`,
`X-CSRF-Token` from `/auth/session`, the cookie, and `Content-Type: application/json`
when a JSON body is required. After login use the **new** CSRF token. The frontend
must handle 401 by showing sign-in, 403 by reloading request verification state,
429 by waiting, and 502/503 as explicit dependency failures. Memory notices pass
through unchanged; they must not depend on the model's reply.

Sessions expire after 30 minutes idle or 24 hours absolute, whichever comes first.
Anonymous login sessions expire after ten minutes. Attempts are limited durably to
five per source address per five minutes and thirty globally per fifteen minutes.
Forwarded IP headers are not trusted. A reverse proxy therefore shares the source
limit; rate limits are deliberately not an identity assertion. Revocation applies
to subsequent request admission; it cannot recall an already admitted operation.
No request is automatically retried after a service failure.

The password accepts 15–1024 characters, including spaces and Unicode. A random
16-byte salt and scrypt verifier (N=131072, r=8, p=1) are stored in SQLite;
password hashes run one at a time to bound memory. Session bearer tokens are random
256-bit values; only SHA-256 hashes are stored. Password reset and session rotation
are transactional. A reset racing a slow password check cannot mint a stale session.
Backups remain sensitive: they include the password verifier and service secrets.

## Running and recovery

`python -m gateway serve` runs HTTPS on port 8050 and requires `GATEWAY_ORIGIN`
(an exact HTTPS origin without a trailing slash) and a distinct `PI_GATEWAY_KEY`
of at least 32 characters. Optional service addresses are `GATEWAY_PI_URL`
and `GATEWAY_TOOLGATE_URL`; defaults are `http://pi:8050` and
`http://toolgate-api:8010`. `GATEWAY_TOOLGATE_OWNER_KEY` is issued by ToolGate,
never an admin or execution key. Without it, owner operations return 503.
`GATEWAY_DB_PATH` defaults to `/auth/auth.db`. Configuration comes from environment
variables; no configuration file or inherited proxy environment is read by the gateway.

The Pi worker gets `PI_GATEWAY_KEY_SHA256`, containing the SHA-256 hex digest of
the gateway runtime key. Its existing `PI_ADMIN_KEY` remains a host recovery
credential. Never give the worker the gateway's environment file or `/auth` volume.
The standalone Pi Compose file remains an authenticated development/recovery API;
the companion Compose file provides the browser deployment boundary.

On the host, `python -m gateway setup --db PATH` sets the initial password;
`reset-password` replaces it and revokes all browser sessions. Both prompt without
echo and explain what the password protects. There is no network setup or reset
endpoint, recovery email, or claim-by-first-visitor behavior. `revoke-all` can be
used without changing the password. In companion these are `./conker auth ...`.

## Optional same-origin dashboard assets

Set `GATEWAY_DASHBOARD_DIR` to an absolute directory containing a trusted,
prebuilt dashboard's `index.html` and assets. With this variable unset the gateway
remains API-only. No frontend source, build tooling or owner configuration is
copied into Pi. The deployment owns building and supplying the directory, for
example as an immutable read-only volume mounted at `/dashboard/dist`. The
existing Pi image and Dockerfile need no dashboard-specific build dependency.

The gateway snapshots supported public build files at startup. Restart it after
replacing a build; modifying files underneath a running process does not change
the served publication. Startup rejects a missing index, relative directory,
symlinks or path escapes, more than 4,096 directory entries, assets over 16 MiB,
or more than 128 MiB of supported asset bytes. Only `index.html`, JavaScript,
CSS, local fonts, images, bounded media, plain-text receipts and web manifests
are served. Other HTML files, JSON configuration, source maps, hidden files,
databases and arbitrary file extensions are not published. Keep credentials,
owner data, uploads and generated applications out of this trusted build directory.

The public shell and assets do not create a login session or contain injected
owner data. API authorization remains mandatory. Only GET/HEAD extensionless
navigation accepting `text/html` receives the SPA shell. Missing assets and
unknown `/auth`, `/api` or `/health` paths never fall back to HTML. A path named
like an asset, including a nonexistent `.js` file, returns 404. Traversal,
encoded traversal and Windows path syntax cannot select host files: request
handling selects vetted bytes from memory and never opens a supplied path.

API/error responses retain `default-src 'none'`. Dashboard responses allow only
same-origin scripts and connections; no inline/evaluated scripts, third-party
scripts or frames are allowed. Inline styles support semantic theme variables,
positioned controls and trusted renderers. Fonts stay local. Images allow local,
`data:` and `blob:` sources for owner-supplied previews, and media allows local
and `blob:` sources for explicit local playback. These source permissions do not
enable uploads, generated applications, model calls or any service authority.
The frontend build must avoid external font/analytics loading and be tested under
this CSP. `Cache-Control: no-store`, `nosniff`, the exact HTTPS host check, cookie
flags, CSRF and Origin checks remain unchanged.

The companion build inspected on 2026-09-20 fits these file limits and packages
its entry scripts, styles and renderer fonts locally. Its theme font loader still
requests Google Fonts; this CSP blocks that request and leaves fallback fonts.
Disable that external loader or package the required theme fonts locally before
claiming a CSP-clean frontend release. Do not expand the gateway policy to admit
third-party font services.

This is an asset-hosting prerequisite only: supplying a dashboard build does not
connect its fixture adapter to live services, enable unsupported CRUD, implement
idle-screen locking or add recent-authentication/step-up enforcement.

The gateway creates a local TLS identity once, in the auth volume. Export its public
certificate with `certificate` and trust that certificate on the client before
login; do not disable certificate verification. It is valid for one year.
`renew-certificate` creates a new identity explicitly; restart the gateway and
trust the newly exported certificate. Missing, mismatched or expired TLS material
causes startup to fail. Renewal interrupted between file replacements fails closed;
rerun the host renewal command. This does not reset the password or recover data.

## Verification and remaining gates

Run `python -m pytest tests/test_gateway_*.py -q` and
`python scripts/auth_mutation_drill.py`. On shells without glob expansion, name
the gateway test files explicitly. `tests/test_gateway_dashboard.py` uses packaged
stub assets and an in-process transport to check publication, CSP, path rejection,
and unchanged authentication boundaries without starting a network listener.
The live test starts real HTTPS and Pi
processes, saves a conversation, rejects a logged-out cookie, and verifies host
password recovery. Only terminal input is supplied by a test harness.

ToolGate's `/v2/owner/...` routes do **not yet exist in the inspected checkout**.
The forwarding contract has been tested against a service fixture, not presented
as a working owner approval round trip. Its implementation and live negative test
(an execution key cannot approve) remain a dependency owned by the ToolGate instance.
Publish a versioned Pi image containing this package before activating companion's
gateway deployment. No release tag or published-image claim is made by this branch.
