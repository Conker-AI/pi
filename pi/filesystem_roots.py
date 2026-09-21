"""Configured file-root metadata through ToolGate, never direct filesystem access."""

import json
import re
import time

import httpx

from .system_actions import ActionError


def _path(value):
    return (
        isinstance(value, str)
        and value.startswith("/")
        and len(value) <= 4097
        and (
            value == "/"
            or all(
                0 < len(part) <= 255
                and part not in (".", "..")
                and not any(
                    c == "\\" or ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF
                    for c in part
                )
                for part in value[1:].split("/")
            )
        )
    )


def read(gate, *, transport=None):
    if gate is None:
        raise ActionError("unconfigured", "ToolGate file access is not configured.", 503)
    try:
        deadline = time.monotonic() + 10
        with (
            httpx.Client(
                transport=transport, trust_env=False, follow_redirects=False, timeout=5
            ) as client,
            client.stream(
                "GET",
                gate.base_url + "/v2/agent/system/file-roots",
                headers={**gate._headers(), "Accept-Encoding": "identity"},
            ) as response,
        ):
            if (
                response.status_code != 200
                or response.headers.get("content-encoding", "identity").lower() != "identity"
            ):
                raise ValueError("unavailable")
            raw = bytearray()
            for chunk in response.iter_raw():
                if len(raw) + len(chunk) > 64000 or time.monotonic() > deadline:
                    raise ValueError("response limit")
                raw.extend(chunk)
            if time.monotonic() > deadline:
                raise ValueError("deadline")
        value = json.loads(raw)
        if value.get("mode") == "unavailable":
            if value.get("roots") != [] or value.get("code") not in (
                "disabled",
                "not_configured",
                "invalid_configuration",
                "unsupported_platform",
            ):
                raise ValueError("invalid unavailable state")
            return {"mode": "unavailable", "code": value["code"], "roots": []}
        capabilities = value.get("capabilities")
        if (
            value.get("mode") != "configured"
            or not isinstance(capabilities, dict)
            or capabilities.get("list") is not True
            or capabilities.get("read") is not False
            or capabilities.get("write") is not False
            or not isinstance(value.get("roots"), list)
            or not 1 <= len(value["roots"]) <= 64
        ):
            raise ValueError("invalid root catalogue")
        roots = []
        for row in value["roots"]:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", row["id"])
                or not _path(row.get("path"))
            ):
                raise ValueError("invalid root")
            roots.append({"id": row["id"], "path": row["path"]})
        if len({row["id"] for row in roots}) != len(roots):
            raise ValueError("duplicate root")
        return {
            "mode": "configured",
            "roots": roots,
            "capabilities": {"list": True, "read": False, "write": False},
        }
    except Exception:
        raise ActionError("roots_unavailable", "File root metadata is unavailable.", 503) from None
