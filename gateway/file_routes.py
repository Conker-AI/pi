"""Owner files through Pi: downloads of artifacts and chat answers, and ChatGPT and Claude imports.

These are the browser's only byte-stream routes; each goes through the one relay in `relay.py`.
They sit before the JSON control relay (`/api/control/pi/{path}`), which cannot carry a file.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request

from pi.api.browser_contract import owner_allowed, owner_download_allowed, session_only_write
from pi.content.office_exports import MIME
from pi.memory.import_formats import MAX_BYTES as MAX_IMPORT_BYTES

from . import relay
from .store import AuthError

MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024  # Pi's own cap on a rendered document
DOWNLOAD_SECONDS = 120  # rendering waits for one of Pi's two export workers (40 s each)
IMPORT_SECONDS = 660  # a large import saves thousands of chats; as long as the CLI waits
# Format -> (Pi's media type, what the browser gets, Pi's format). CSV and JSON are an artifact's
# native export, which Pi sends only for a table (CSV) or a chart, diagram or media (JSON).
DOCUMENTS = {name: (MIME[name], MIME[name], name) for name in ("docx", "pdf", "pptx", "xlsx")}
NATIVE = {
    "csv": ("text/csv", "text/csv; charset=utf-8", "native"),
    "json": ("application/json", "application/json; charset=utf-8", "native"),
}
ARTIFACT_FORMATS = {**DOCUMENTS, **NATIVE}
ANSWER_FORMATS = DOCUMENTS
SOURCES = ("chatgpt", "claude")


def too_large() -> AuthError:
    limit = MAX_IMPORT_BYTES // (1024 * 1024)
    return AuthError(f"The file is larger than {limit} MB.", 413, code="too_large")


def router(app, session, admit_write, stream_transport):
    routes = APIRouter()

    def owner_key() -> str:
        if not app.state.config.pi_owner_key:
            raise AuthError("Pi owner-control connection is not configured.", 503)
        return app.state.config.pi_owner_key

    async def download(request: Request, target: str, formats: dict, versioned: bool = False):
        session(request)
        params = request.query_params
        extra = {"version"} if versioned else set()
        if (
            set(params) - extra != {"format"}
            or len(params.getlist("format")) != 1
            or len(params.getlist("version")) > 1
            or params["format"] not in formats
            or ("version" in params and not re.fullmatch(r"[1-9][0-9]?|100", params["version"]))
        ):
            raise AuthError("Choose one format: " + ", ".join(formats) + ".", 422)
        if not owner_download_allowed("GET", target):
            raise AuthError("This file is not available through the gateway.", 403)
        name = params["format"]
        media, content_type, upstream_format = formats[name]
        sent = {"format": upstream_format}
        if "version" in params:
            sent["version"] = int(params["version"])
        return await relay.relay(
            stream_transport,
            "GET",
            app.state.config.pi_url.rstrip("/") + target,
            {"X-Pi-Owner-Key": owner_key()},
            params=sent,
            read_seconds=DOWNLOAD_SECONDS,
            download=relay.Download(
                media,
                content_type,
                name,
                MAX_DOWNLOAD_BYTES,
                other_type="Choose a format supported by this artifact."
                if upstream_format == "native"
                else None,
            ),
        )

    @routes.get("/api/control/pi/artifacts/{identity}/download")
    async def artifact_download(identity: str, request: Request):
        """One artifact as a file: DOCX, PDF, PPTX or XLSX, or CSV for a table and JSON for a
        chart, diagram or media. An optional `version` (1 to 100) exports an earlier version."""
        if not re.fullmatch(r"artifact_[0-9a-f]{32}", identity):
            session(request)
            raise AuthError("Invalid artifact identity.", 422)
        return await download(
            request, f"/artifacts/{identity}/download", ARTIFACT_FORMATS, versioned=True
        )

    @routes.get("/api/control/pi/sessions/{session_id}/messages/{message_id}/download")
    async def answer_download(session_id: str, message_id: str, request: Request):
        """One completed chat answer as a DOCX, PDF, PPTX or XLSX file."""
        if not re.fullmatch(r"ses_[0-9a-f]{16}", session_id) or not re.fullmatch(
            r"msg_[0-9a-f]{16}", message_id
        ):
            session(request)
            raise AuthError("Invalid chat or answer identity.", 422)
        return await download(
            request, f"/sessions/{session_id}/messages/{message_id}/download", ANSWER_FORMATS
        )

    @routes.post("/api/control/pi/imports/{source}/{step}")
    async def import_file(source: str, step: str, request: Request):
        """A ChatGPT or Claude export's `conversations.json` as the raw body, up to 128 MB.

        `preview` saves nothing, so the signed-in session is enough. An import (named by the
        previewed file's SHA-256) saves chats and offers his words to memory, so it asks for the
        password: the proof covers this path with an empty body, the path's digest names the file,
        and a body with another digest is refused before Pi receives all of it.
        """
        session(request)
        target = f"/imports/{source}/{step}"
        if (
            source not in SOURCES
            or not (step == "preview" or re.fullmatch(r"[0-9a-f]{64}", step))
            or not owner_allowed("POST", target)
            or request.scope["query_string"]
        ):
            raise AuthError(
                "Choose chatgpt or claude, then preview or the previewed file's digest, "
                "without query parameters.",
                422,
            )
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            raise AuthError("Send the export's conversations.json as application/json.", 415)
        declared = request.headers.get("content-length")
        if declared is not None:
            if not declared.isdecimal():
                raise AuthError("Invalid content length.", 400)
            if int(declared) > MAX_IMPORT_BYTES:
                raise too_large()
        key = owner_key()
        if not session_only_write("POST", target):
            admit_write(request, {})
        return await relay.relay(
            stream_transport,
            "POST",
            app.state.config.pi_url.rstrip("/") + target,
            {"X-Pi-Owner-Key": key, "Content-Type": "application/json"},
            read_seconds=IMPORT_SECONDS,
            upload=relay.Upload(
                request,
                MAX_IMPORT_BYTES,
                too_large(),
                digest=None if step == "preview" else step,
            ),
        )

    return routes
