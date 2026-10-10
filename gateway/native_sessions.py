"""Bounded owner-only projection of the native Claude background session store."""

import json
import re
import unicodedata
from typing import Annotated, Literal

import httpx
from pydantic import Field, field_validator, model_validator

from .provider_control import Strict

SESSION_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}"
Identity = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64)]
SessionId = Annotated[str, Field(pattern=rf"^{SESSION_ID}$", min_length=1, max_length=100)]
SafeInt = Annotated[int, Field(ge=0, le=9007199254740991)]
TIMEOUT = 30
CAPS = {"list": 1048576, "status": 65536, "output": 524288, "stop": 65536}
ERRORS = {
    "invalid_native_session_request": (422, "Invalid native session request."),
    "native_session_not_found": (404, "Native session was not found. Refresh the session list."),
    "native_session_changed": (409, "Native session identity changed. Refresh before continuing."),
    "native_session_not_stoppable": (409, "This native session cannot currently be stopped."),
    "native_sessions_unavailable": (
        503,
        "Native session status is unavailable. Check status before repeating an operation.",
    ),
}


class NativeSessionsError(Exception):
    def __init__(self, code="native_sessions_unavailable"):
        self.code = code
        self.status, message = ERRORS[code]
        super().__init__(message)


class Stop(Strict):
    expectedIdentity: Identity


class Session(Strict):
    id: SessionId
    identity: Identity
    sessionId: SessionId | None
    name: str | None = Field(max_length=300)
    cwd: str = Field(min_length=1, max_length=4096)
    startedAt: SafeInt
    state: Literal["working", "blocked", "done", "failed", "stopped", "unknown"]
    processStatus: Literal["busy", "waiting", "idle"] | None
    waitingFor: (
        Literal[
            "permission_prompt", "input_needed", "sandbox_request", "worker_request", "dialog_open"
        ]
        | None
    )
    canStop: bool

    @field_validator("name", "cwd")
    @classmethod
    def safe_metadata(cls, value):
        if value is not None and any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
        ):
            raise ValueError("Unsafe native metadata.")
        return value

    @model_validator(mode="after")
    def stoppable_state(self):
        if self.canStop and self.state not in {"working", "blocked"}:
            raise ValueError("Unconfirmed native stop authority.")
        return self


class Evidence(Strict):
    schemaVersion: Literal[1]
    provider: Literal["claude"]

    @model_validator(mode="before")
    @classmethod
    def exact_constants(cls, value):
        if not isinstance(value, dict) or type(value.get("schemaVersion")) is not int:
            raise ValueError("Invalid evidence version.")
        for field in ("outputPersisted", "automaticReplay"):
            if field in value and value[field] is not False:
                raise ValueError("Invalid evidence constant.")
        return value


class Catalogue(Evidence):
    scope: Literal["background_sessions"]
    results: list[Session] = Field(max_length=100)
    truncated: bool
    omittedCount: SafeInt

    @model_validator(mode="after")
    def bounded_catalogue(self):
        if len({row.id for row in self.results}) != len(self.results):
            raise ValueError("Duplicate native identity.")
        return self


class Status(Evidence):
    result: Session


class Output(Evidence):
    id: SessionId
    identity: Identity
    text: str
    truncated: bool
    outputPersisted: Literal[False]

    @field_validator("text")
    @classmethod
    def bounded_text(cls, text):
        if len(text.encode("utf-8")) > 65536 or any(
            character not in "\t\r\n" and unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in text
        ):
            raise ValueError("Native output exceeds its byte limit.")
        return text


class Receipt(Evidence):
    id: SessionId
    identity: Identity
    status: Literal["stop_requested"]
    automaticReplay: Literal[False]


def _json(raw):
    def pairs(items):
        value = {}
        for key, child in items:
            if key in value:
                raise ValueError("Duplicate JSON key.")
            value[key] = child
        return value

    def constant(_):
        raise ValueError("Non-finite JSON value.")

    return json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)


def request(transport, action, identity=None, body=None):
    if action not in CAPS or (action == "list") != (identity is None):
        raise NativeSessionsError("invalid_native_session_request")
    if identity is not None and (
        not isinstance(identity, str) or not re.fullmatch(SESSION_ID, identity)
    ):
        raise NativeSessionsError("invalid_native_session_request")
    try:
        if action == "stop":
            body = Stop.model_validate(body).model_dump()
        elif body is not None:
            raise ValueError()
    except ValueError:
        raise NativeSessionsError("invalid_native_session_request") from None
    if transport is None:
        raise NativeSessionsError()
    path = "/claude/sessions" + (f"/{identity}" if identity else "")
    if action in {"output", "stop"}:
        path += f"/{action}"
    try:
        with (
            httpx.Client(
                transport=transport, timeout=TIMEOUT, trust_env=False, follow_redirects=False
            ) as client,
            client.stream(
                "POST" if action == "stop" else "GET",
                "http://conker-host" + path,
                **({"json": body} if body is not None else {}),
            ) as response,
        ):
            if 300 <= response.status_code < 400:
                raise ValueError()
            raw = bytearray()
            cap = CAPS[action] if response.status_code == 200 else 4096
            for chunk in response.iter_bytes(chunk_size=8192):
                if len(raw) + len(chunk) > cap:
                    raise ValueError()
                raw.extend(chunk)
            value = _json(raw)
            if response.status_code != 200:
                code = value.get("error") if isinstance(value, dict) else None
                if (
                    set(value) == {"error"}
                    and code in ERRORS
                    and ERRORS[code][0] == response.status_code
                ):
                    raise NativeSessionsError(code)
                raise ValueError()
            model = {"list": Catalogue, "status": Status, "output": Output, "stop": Receipt}[action]
            result = model.model_validate(value)
            if action != "list":
                returned = result.result.id if action == "status" else result.id
                if returned != identity:
                    raise ValueError()
            if action == "stop" and result.identity != body["expectedIdentity"]:
                raise ValueError()
            return result.model_dump()
    except (httpx.HTTPError, ValueError, UnicodeError, RecursionError, TypeError):
        raise NativeSessionsError() from None
