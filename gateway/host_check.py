"""`python -m gateway check-password`: the owner's password checked for a host change that no
service can ask it for.

`conker providers spending on|off|recover` writes the paid-model policy into the host's own files
and restarts Pi: no service sits between the command and the change. Settings asks the password for
it (`/api/host/providers`, `paid-policy`), so the CLI asks too and has it checked here before it
changes anything. The password comes once, as `{"password"}` on stdin, never in argv or the
environment; `AuthStore.host_password` checks it with the host budget (five wrong per five
minutes, shared with the other host commands). The answer is only `{"checked": true}`; nothing
here repeats the password or the request in an error. It guards the command, not the host: whoever
controls the host can edit those files directly.
"""

from __future__ import annotations

import json
import sys
from typing import BinaryIO, TextIO

from .store import AuthError, AuthStore

MAX_REQUEST_BYTES = 4096
NOTHING = " Nothing was changed."


def run(auth: AuthStore, stdin: BinaryIO, out: TextIO, err: TextIO) -> int:
    raw = stdin.read(MAX_REQUEST_BYTES + 1)
    try:
        value = json.loads(raw.decode("utf-8")) if len(raw) <= MAX_REQUEST_BYTES else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = None
    password = (
        value.get("password") if isinstance(value, dict) and set(value) == {"password"} else None
    )
    try:
        if not isinstance(password, str) or not password:
            raise AuthError("Send only the password, as one JSON object.")
        auth.host_password(password)
    except AuthError as exc:
        print(f"Gateway did not complete: {exc}{NOTHING}", file=err)
        return 1
    print(json.dumps({"checked": True}), file=out)
    return 0


def main(db: str) -> int:
    return run(AuthStore(db), sys.stdin.buffer, sys.stdout, sys.stderr)
