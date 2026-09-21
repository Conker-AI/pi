"""Canonical, content-free bindings for explicitly allowed browser writes."""

import hashlib
import json
import math
import re

from pi.browser_contract import runtime_allowed, owner_allowed

from .store import AuthError


def _invalid() -> AuthError:
    return AuthError("Send a bounded, unambiguous JSON object.", 422)


def validate_json(value, depth: int = 0) -> None:
    if depth > 32:
        raise _invalid()
    if isinstance(value, dict):
        for key, child in value.items():
            validate_json(key, depth + 1)
            validate_json(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            validate_json(child, depth + 1)
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeError:
            raise _invalid() from None
    elif isinstance(value, float) and not math.isfinite(value):
        raise _invalid()


def parse_object(raw: bytes | bytearray) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("constant")

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        validate_json(value)
    except (ValueError, UnicodeError, RecursionError):
        raise _invalid() from None
    if not isinstance(value, dict):
        raise _invalid()
    return value


def fingerprint(method: str, path: str, body: dict) -> str:
    allowed = (
        method == "POST"
        and isinstance(path, str)
        and len(path) <= 512
        and (
            (path.startswith("/api/pi/") and runtime_allowed(method, path[len("/api/pi") :]))
            or (path.startswith("/api/control/pi/") and owner_allowed(method, path[len("/api/control/pi") :]))
            or re.fullmatch(r"/api/owner/requests/[A-Za-z0-9_-]+/decision", path)
            or path == "/api/terminal"
        )
    )
    if not allowed or not isinstance(body, dict):
        raise AuthError("This operation is not available for password verification.", 422)
    validate_json(body)
    canonical = json.dumps(
        [method, path, body],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()
