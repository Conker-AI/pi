"""Bounded, metadata-only ToolGate port review transport for the owner API."""

import ipaddress
import json
import math
import re
import time
from typing import Literal

import httpx
from pydantic import Field, StrictInt, field_validator, model_validator

from .agents import StrictModel
from .system_actions import ActionError


class Mapping(StrictModel):
    hostAddress: str = Field(min_length=1, max_length=45)
    hostPort: StrictInt = Field(ge=1, le=65535)
    containerPort: StrictInt = Field(ge=1, le=65535)
    protocol: Literal["tcp", "udp"]

    @field_validator("hostAddress")
    @classmethod
    def address(cls, value):
        return str(ipaddress.ip_address(value))


class Request(StrictModel):
    container_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    operation: Literal["create", "edit", "remove"]
    mapping: Mapping | None = None
    original: Mapping | None = None

    @model_validator(mode="after")
    def validate_operation(self):
        if (self.mapping is not None) != (self.operation in ("create", "edit")):
            raise ValueError("Mapping must match operation")
        if (self.original is not None) != (self.operation in ("edit", "remove")):
            raise ValueError("Original must match operation")
        if self.mapping and self.mapping.hostAddress not in ("127.0.0.1", "0.0.0.0"):
            raise ValueError("Use a supported binding address")
        return self


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _mappings(value):
    if not isinstance(value, list) or len(value) > 200:
        raise ValueError("invalid mappings")
    result = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("invalid mapping")
        # Deliberately project known fields, dropping upstream debug/private data.
        result.append(
            Mapping.model_validate(
                {key: item[key] for key in ("hostAddress", "hostPort", "containerPort", "protocol")}
            ).model_dump()
        )
    identities = [json.dumps(item, sort_keys=True) for item in result]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate mapping")
    return sorted(result, key=lambda item: json.dumps(item, sort_keys=True))


def _project(value, *, request=None, review_id=None):
    rid, expires = value["reviewId"], value["expiresAt"]
    if (
        not isinstance(rid, str)
        or not re.fullmatch(r"[a-f0-9]{48}", rid)
        or (review_id is not None and rid != review_id)
        or type(expires) not in (int, float)
        or not math.isfinite(expires)
        or expires <= 0
        or expires > time.time() + 630
        or type(value.get("consumed")) is not bool
        or type(value.get("expired")) is not bool
    ):
        raise ValueError("invalid review")
    preview = value["preview"]
    cid, operation = preview["containerId"], preview["operation"]
    if (
        not isinstance(cid, str)
        or not re.fullmatch(r"[a-f0-9]{64}", cid)
        or operation not in ("create", "edit", "remove")
        or preview.get("execution") != "owner_approval_required"
        or preview.get("hostAvailability") != "not_checked"
        or preview.get("bindingSource") not in ("observed", "configured")
        or preview.get("writableLayer") != "snapshot_required"
        or preview.get("volumeData") != "reuse_existing"
        or preview.get("tmpfsData") not in ("none", "reset_on_replacement")
    ):
        raise ValueError("invalid preview")
    before, after = _mappings(preview["before"]), _mappings(preview["after"])
    changed = before != after
    if (
        preview.get("changed") is not changed
        or preview.get("requiresReplacement") is not changed
        or type(preview.get("downtimeExpected")) is not bool
        or (preview["downtimeExpected"] and not changed)
    ):
        raise ValueError("invalid replacement flags")
    if request:
        if (
            cid != request.container_id
            or operation != request.operation
            or value["consumed"]
            or value["expired"]
        ):
            raise ValueError("request mismatch")
        expected = list(before)
        if request.original:
            expected.remove(request.original.model_dump())
        if request.mapping:
            expected.append(request.mapping.model_dump())
        if _mappings(expected) != after:
            raise ValueError("mapping delta mismatch")
    projected = {
        key: preview[key]
        for key in (
            "containerId",
            "operation",
            "execution",
            "hostAvailability",
            "bindingSource",
            "writableLayer",
            "volumeData",
            "tmpfsData",
            "changed",
            "requiresReplacement",
            "downtimeExpected",
        )
    }
    return {
        "reviewId": rid,
        "expiresAt": expires,
        "consumed": value["consumed"],
        "expired": value["expired"] or expires <= time.time(),
        "preview": {**projected, "before": before, "after": after},
    }


def fetch(gate, *, request=None, review_id=None, transport=None):
    if gate is None:
        raise ActionError("unconfigured", "ToolGate port review is not configured.", 503)
    if (request is None) == (review_id is None):
        raise ActionError("invalid_request", "Select a review or provide a port change.", 422)
    if request is not None:
        request = Request.model_validate(request.model_dump())
    elif not isinstance(review_id, str) or not re.fullmatch(r"[a-f0-9]{48}", review_id):
        raise ActionError("invalid_review", "Invalid port review identity.", 422)
    path = "/v2/agent/system/port-reviews" + ("/" + review_id if review_id else "")
    try:
        deadline = time.monotonic() + 15
        options = {"json": request.model_dump(exclude_none=True)} if request else {}
        with (
            httpx.Client(
                transport=transport, trust_env=False, follow_redirects=False, timeout=10
            ) as client,
            client.stream(
                "POST" if request else "GET",
                gate.base_url + path,
                headers={**gate._headers(), "Accept-Encoding": "identity"},
                **options,
            ) as response,
        ):
            if (
                response.status_code != 200
                or response.headers.get("content-encoding", "identity") != "identity"
            ):
                raise ValueError("unavailable")
            raw = bytearray()
            for chunk in response.iter_raw():
                if len(raw) + len(chunk) > 256 * 1024 or time.monotonic() > deadline:
                    raise ValueError("response limit")
                raw.extend(chunk)
            if time.monotonic() > deadline:
                raise ValueError("deadline")
        value = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError()),
        )
        return _project(value, request=request, review_id=review_id)
    except Exception:
        raise ActionError(
            "review_unavailable", "Port review is unavailable; no change was executed.", 503
        ) from None
