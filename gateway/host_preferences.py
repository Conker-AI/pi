"""`python -m gateway owner-preferences`: the host CLI's way to save a preference that asks for the
owner's password (ADR-0010): the sign-out time, the access a new chat starts with, the daily budgets.

`conker config set` saves preferences on Pi's recovery key. Pi refuses one that would change any
of `pi.core.owner_preferences.PASSWORD_FIELDS` (403 `password_required`) and saves nothing; the CLI
then asks for the password and sends the same save here once, with the password, as one JSON
object on stdin, never in argv or the environment: `{"password", "expected_revision",
"preferences"}`. `AuthStore.host_password` checks it (the host budget: five wrong per five
minutes), then the save goes to Pi with the owner credential, as the Settings screen's does after
its password prompt, at the CLI's revision, so a change made meanwhile is refused, never
overwritten. The sign-out time Pi saved is the one this gateway enforces from then on. Nothing
here repeats the password or the request in an error.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from typing import BinaryIO, TextIO

import httpx

from .spending_cap import NOTHING_SAVED, call_preferences
from .spending_cap import CapError as NotSaved
from .store import AuthError, AuthStore

MAX_REQUEST_BYTES = 16384
MAYBE_SAVED = " It may already be saved; check with conker config get before trying again."


def read_request(stream: BinaryIO) -> tuple[str, dict]:
    """The password and the save (expected revision and preferences), checked before any use.

    Pi validates the preferences themselves with its own model when they arrive.
    """
    raw = stream.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        raise NotSaved("The request is too large." + NOTHING_SAVED)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise NotSaved(
            "Send the password and the save as one JSON object." + NOTHING_SAVED
        ) from None
    if (
        not isinstance(value, dict)
        or set(value) != {"password", "expected_revision", "preferences"}
        or not isinstance(value["password"], str)
        or not value["password"]
        or type(value["expected_revision"]) is not int
        or value["expected_revision"] < 1
        or not isinstance(value["preferences"], dict)
    ):
        raise NotSaved(
            "Send a password, the revision you read and the preferences to save." + NOTHING_SAVED
        )
    return value["password"], {
        "expected_revision": value["expected_revision"],
        "preferences": value["preferences"],
    }


def run(config, auth: AuthStore, client: httpx.Client, stdin: BinaryIO, out: TextIO, err: TextIO):
    try:
        password, body = read_request(stdin)
        if not config.pi_owner_key:
            raise NotSaved("Pi owner-control connection is not configured." + NOTHING_SAVED)
        try:
            auth.host_password(password)
        except AuthError as exc:
            raise NotSaved(str(exc) + NOTHING_SAVED) from None
        saved = call_preferences(client, config, "POST", body, MAYBE_SAVED)
        try:
            # Pi's answer, recorded after it came back: a newer save from Settings may already be
            # kept, so, as for a read, an older revision may only shorten the window.
            minutes = saved["preferences"].get("idleTimeoutMinutes")
            auth.owner_window(saved["revision"], minutes, saved=False)
        except (sqlite3.Error, OSError):
            raise NotSaved(
                "The gateway could not record the sign-out time." + MAYBE_SAVED
            ) from None
    except NotSaved as exc:
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
