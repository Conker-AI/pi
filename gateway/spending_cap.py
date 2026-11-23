"""`python -m gateway spending-cap`: the host CLI's way to change the daily spending cap (R-M6).

The owner's password and the change arrive together as one JSON object on stdin, never in argv
or the environment, and are used once: `AuthStore.host_password` checks the password, then the
cap is saved with the owner credential at the revision just read, as the Settings screen does
after its password prompt (ADR-0010). Pi refuses a cap change on its recovery key, so this is the
host's way to make one. Nothing here repeats the password or the request in an error.
"""

from __future__ import annotations

import json
import sys
from typing import BinaryIO, TextIO

import httpx

from .store import AuthError, AuthStore

MAX_REQUEST_BYTES = 4096
LIMITS = {"cents": 100_000, "tokens": 100_000_000}
NOTHING_SAVED = " Nothing was saved."
MAYBE_SAVED = " It may already be saved; check with conker spending before trying again."


class CapError(ValueError):
    pass


def read_request(stream: BinaryIO) -> tuple[str, dict]:
    """The password and the cap's changes (enabled, cents, tokens), checked before any use."""
    raw = stream.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise CapError("The request is too large." + NOTHING_SAVED)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CapError(
            "Send the password and the change as one JSON object." + NOTHING_SAVED
        ) from None
    changes = value.get("changes") if isinstance(value, dict) else None
    if (
        not isinstance(value, dict)
        or set(value) != {"password", "changes"}
        or not isinstance(value["password"], str)
        or not value["password"]
        or not isinstance(changes, dict)
        or not changes
        or not set(changes) <= {"enabled", *LIMITS}
        or ("enabled" in changes and type(changes["enabled"]) is not bool)
        or any(
            key in changes and (type(changes[key]) is not int or not 0 <= changes[key] <= top)
            for key, top in LIMITS.items()
        )
    ):
        raise CapError(
            "Send a password and the cap's changes: enabled on or off, or whole cents and tokens "
            "within the cap's limits." + NOTHING_SAVED
        )
    return value["password"], changes


def call_preferences(
    client: httpx.Client, config, method: str, body: dict | None = None, maybe: str = MAYBE_SAVED
) -> dict:
    """Pi's owner preferences with the owner credential (also `owner-preferences`). `maybe` is what
    a save whose answer was lost tells the owner to check."""
    after = NOTHING_SAVED if body is None else maybe
    try:
        response = client.request(
            method,
            config.pi_url.rstrip("/") + "/owner/preferences",
            headers={"X-Pi-Owner-Key": config.pi_owner_key, "Accept": "application/json"},
            json=body,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise CapError("Pi did not answer." + NOTHING_SAVED) from None
    except (httpx.HTTPError, OSError):
        raise CapError("Pi gave no complete answer." + after) from None
    if response.status_code == 409:
        raise CapError("The preferences changed while the command ran." + NOTHING_SAVED)
    if response.status_code != 200:
        raise CapError(f"Pi refused it (HTTP {response.status_code})." + NOTHING_SAVED)
    try:
        value = response.json()
        if (
            type(value["revision"]) is int
            and isinstance(value["preferences"], dict)
            and isinstance(value["preferences"]["spendingCap"], dict)
        ):
            return value
    except (ValueError, KeyError, TypeError):
        pass
    raise CapError("Pi answered with something unreadable." + after)


def save_cap(config, client: httpx.Client, changes: dict) -> dict:
    """Read the preferences, change only the cap, save at that revision."""
    current = call_preferences(client, config, "GET")
    preferences = dict(current["preferences"])
    preferences["spendingCap"] = {**preferences["spendingCap"], **changes}
    saved = call_preferences(
        client,
        config,
        "POST",
        {"expected_revision": current["revision"], "preferences": preferences},
    )
    return {"revision": saved["revision"], "spendingCap": saved["preferences"]["spendingCap"]}


def run(config, auth: AuthStore, client: httpx.Client, stdin: BinaryIO, out: TextIO, err: TextIO):
    try:
        password, changes = read_request(stdin)
        if not config.pi_owner_key:
            raise CapError("Pi owner-control connection is not configured." + NOTHING_SAVED)
        try:
            auth.host_password(password)
        except AuthError as exc:
            raise CapError(str(exc) + NOTHING_SAVED) from None
        saved = save_cap(config, client, changes)
    except CapError as exc:
        print(f"Gateway did not complete: {exc}", file=err)
        return 1
    print(json.dumps(saved, sort_keys=True), file=out)
    return 0


def main(db: str) -> int:
    from .api import Config

    config = Config.environment()
    config.validate()
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=30) as client:
        return run(config, AuthStore(db), client, sys.stdin.buffer, sys.stdout, sys.stderr)
