"""Bounded browser operations for ToolGate's remote MCP tools."""

import json
import re
from urllib.parse import urlsplit

from .store import AuthError

TOOL_ID = r"mcp\.[a-z0-9][a-z0-9.-]{1,74}"


def validate_body(operation: str, body: dict) -> None:
    required = {
        "discover": {"url"},
        "import": {"id", "name", "description", "url", "tool_name", "input_schema", "schema_hash"},
        "state": {"enabled", "expected_digest"},
        "access": {"enabled", "expected_digest"},
    }[operation]
    optional = {"secret_ref"} if operation in {"discover", "import"} else set()
    valid = required <= body.keys() and not body.keys() - required - optional
    if operation in {"state", "access"}:
        valid = (
            valid
            and type(body.get("enabled")) is bool
            and bool(
                isinstance(body.get("expected_digest"), str)
                and re.fullmatch(r"[a-f0-9]{64}", body["expected_digest"])
            )
        )
    else:
        try:
            url = body.get("url", "")
            parsed = urlsplit(url)
            valid = (
                valid
                and isinstance(url, str)
                and 1 <= len(url) <= 2048
                and (
                    parsed.scheme == "https"
                    and bool(parsed.hostname)
                    and not parsed.username
                    and not parsed.password
                    and parsed.port in {None, 443}
                    and not parsed.query
                    and not parsed.fragment
                    and not any(ord(character) < 33 for character in url)
                    and "\\" not in url
                )
            )
        except (ValueError, TypeError, AttributeError):
            valid = False
        secret = body.get("secret_ref")
        valid = valid and (
            secret is None
            or (isinstance(secret, str) and bool(re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", secret)))
        )
    if operation == "import":
        valid = (
            valid
            and isinstance(body.get("id"), str)
            and bool(re.fullmatch(TOOL_ID, body["id"]))
            and all(
                isinstance(body.get(key), str)
                and bool(body[key].strip())
                and len(body[key]) <= limit
                for key, limit in (("name", 120), ("description", 4000), ("tool_name", 128))
            )
            and isinstance(body.get("input_schema"), dict)
            and bool(
                isinstance(body.get("schema_hash"), str)
                and re.fullmatch(r"[a-f0-9]{64}", body["schema_hash"])
            )
        )
        if valid:
            valid = bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", body["tool_name"])) and (
                body["input_schema"].get("type") == "object"
                and len(
                    json.dumps(
                        body["input_schema"], separators=(",", ":"), ensure_ascii=False
                    ).encode()
                )
                <= 32768
            )
    if not valid:
        raise AuthError(
            "Send the reviewed MCP endpoint, tool definition or exact saved digest.", 422
        )
