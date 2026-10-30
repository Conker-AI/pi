"""Server actions through the host helper (ADR-0014) on the private provider-control socket.

The browser may name only the operations below, each with exactly its typed arguments; the
gateway checks them here before anything reaches the helper, and the helper checks them again.
Reads answer at once; actions return the helper's receipt, which the owner's screen follows by
id. Whatever the helper answers, receipt or refusal (`{"error", "message"}`), passes back
unchanged after a size cap and a check that it answers this request. When the helper cannot be
reached the gateway answers in the helper's own refusal shape, so the screen reads one shape.
"""

from __future__ import annotations

import re

import httpx

from .native_sessions import _json

OP_ID = r"[A-Za-z0-9_.:-]{16,128}"
SERVICE = r"[a-z][a-z0-9-]{0,62}"
SNAPSHOT = r"snapshot-\d{8}T\d{6}Z-[0-9a-f]{8}"
OPERATION = r"[a-z]+(?:\.[a-z]+)+"
# Operation -> its arguments and their patterns (conker scripts/host_ops.py OPERATIONS).
READS = {"services.status": {}, "backup.list": {}}
ACTIONS = {
    "backup.verify": {"snapshot": SNAPSHOT},
    "services.restart": {"service": SERVICE},
    "backup.run": {},
}
OUTCOMES = {"working", "done", "failed", "unknown"}
# The helper's refusals, by status: invalid (422), refused or busy (409), unknown id or route
# (404), unavailable (503).
REFUSALS = {404, 409, 422, 503}
MAX_ANSWER = 262144
MAX_REFUSAL = 4096
# Reads run docker and `conker setup protection` (up to 60 s); starting an action checks the
# same facts first. Receipts are read from the journal.
TIMEOUTS = {"receipts": 30, "read": 120, "start": 120}


class HostOpsError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


def invalid(message: str) -> HostOpsError:
    return HostOpsError(422, "invalid_host_operation", message)


def unavailable() -> HostOpsError:
    return HostOpsError(
        503,
        "host_unavailable",
        "The host helper did not answer. Nothing was retried; check the receipts before "
        "trying again.",
    )


def validate(body: object, *, read: bool) -> dict:
    """The browser's read or action, exactly as the allowlist names it."""
    fields = {"operation", "arguments"} if read else {"id", "operation", "arguments"}
    if not isinstance(body, dict) or set(body) != fields:
        raise invalid(
            "Supply only "
            + ("the operation and its arguments." if read else "id, operation and arguments.")
        )
    if not read and not (isinstance(body["id"], str) and re.fullmatch(OP_ID, body["id"])):
        raise invalid("Use an id of 16 to 128 letters, digits or _.:-")
    table = READS if read else ACTIONS
    patterns = table.get(body["operation"]) if isinstance(body["operation"], str) else None
    if patterns is None:
        raise invalid(f"This server {'read' if read else 'action'} is not allowed.")
    arguments = body["arguments"]
    if (
        not isinstance(arguments, dict)
        or set(arguments) != set(patterns)
        or any(
            not isinstance(arguments[key], str) or not re.fullmatch(pattern, arguments[key])
            for key, pattern in patterns.items()
        )
    ):
        raise invalid("Supply exactly this operation's arguments.")
    return body


def receipt(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("id"), str)
        and re.fullmatch(OP_ID, value["id"]) is not None
        and isinstance(value.get("operation"), str)
        and re.fullmatch(OPERATION, value["operation"]) is not None
        and value.get("outcome") in OUTCOMES
    )


def answers(path: str, body: dict | None, value: dict) -> bool:
    """Whether a 200 answer is the answer to this request."""
    if path == "/host/ops" and body is None:
        return (
            value.get("schemaVersion") == 1
            and isinstance(value.get("operations"), list)
            and len(value["operations"]) <= 200
            and all(receipt(row) for row in value["operations"])
            and type(value.get("damagedRecords")) is int
        )
    if path == "/host/read":
        return value.get("operation") == body["operation"] and isinstance(value.get("result"), dict)
    if path == "/host/ops":
        return receipt(value) and all(
            value.get(key) == body[key] for key in ("id", "operation", "arguments")
        )
    return receipt(value) and value["id"] == path.removeprefix("/host/ops/")


def request(transport, socket: str, method: str, path: str, body: dict | None = None):
    """One helper call: (status, JSON) as the helper answered, or HostOpsError."""
    if transport is None and not socket:
        raise HostOpsError(503, "host_unavailable", "Server actions are not set up on this server.")
    kind = "receipts" if method == "GET" else "read" if path == "/host/read" else "start"
    try:
        with (
            httpx.Client(
                transport=transport or httpx.HTTPTransport(uds=socket),
                timeout=TIMEOUTS[kind],
                trust_env=False,
                follow_redirects=False,
            ) as client,
            client.stream(
                method,
                "http://conker-host" + path,
                **({"json": body} if body is not None else {}),
            ) as response,
        ):
            status = response.status_code
            if status != 200 and status not in REFUSALS:
                raise ValueError()
            if response.headers.get("content-type", "").split(";", 1)[0] != "application/json":
                raise ValueError()
            cap = MAX_ANSWER if status == 200 else MAX_REFUSAL
            raw = bytearray()
            for chunk in response.iter_bytes(chunk_size=8192):
                if len(raw) + len(chunk) > cap:
                    raise ValueError()
                raw.extend(chunk)
            value = _json(raw)
            if not isinstance(value, dict):
                raise ValueError()
            if status != 200:
                if (
                    not isinstance(value.get("error"), str)
                    or set(value) - {"error", "message"}
                    or not isinstance(value.get("message", ""), str)
                ):
                    raise ValueError()
            elif not answers(path, body, value):
                raise ValueError()
            return status, value
    except (httpx.HTTPError, OSError, ValueError, UnicodeError, RecursionError, TypeError):
        raise unavailable() from None
