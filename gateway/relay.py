"""The gateway's one byte-stream relay to Pi: files out to the browser, export files in from it.

Everything else the browser reaches is a JSON object of a few kilobytes, relayed whole by the
gateway's `forward`. A file is different: it may run to megabytes, its type and its name matter,
and nothing in it may be read as a page. So this relay streams the bytes without holding them,
under a size cap and fixed timeouts, never follows a redirect, asks for and accepts no transfer
encoding, passes only the content type its caller names, rebuilds Content-Disposition from the
checked parts of Pi's, and marks every file no-store and nosniff. A refusal from Pi (any status
but 200) passes back as Pi's own JSON. No other upstream header crosses to the browser.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from .store import AuthError

CONNECT_SECONDS = 10
MAX_JSON_BYTES = 1024 * 1024  # an answer about a file (an import preview), never the file
MAX_ERROR_BYTES = 65536
# Pi's names: office_exports.names() plus a version and an extension; the readable one is
# percent-encoded UTF-8 (RFC 6266 filename*).
ASCII_NAME = r"[A-Za-z0-9_][A-Za-z0-9._-]{0,199}"
READABLE_NAME = r"(?:[A-Za-z0-9_.~-]|%[0-9A-F]{2}){1,1200}"
DISPOSITION = re.compile(
    rf"attachment; filename=\"({ASCII_NAME})\"(?:; filename\*=UTF-8''({READABLE_NAME}))?"
)


@dataclass(frozen=True)
class Download:
    """What a download must be: Pi's media type for it, the Content-Type the browser gets, the
    file name's extension and the size cap. With `other_type`, a file of another type is that
    refusal (Pi's own shape) instead of a gateway failure."""

    media: str
    content_type: str
    extension: str
    max_bytes: int
    other_type: str | None = None


def unavailable() -> AuthError:
    return AuthError(
        "Pi is unavailable; check conker status. Nothing was retried; check the result before "
        "trying again.",
        503,
    )


def unreadable() -> AuthError:
    return AuthError("Pi returned an unexpected answer. Check its logs before retrying.", 502)


class Upload:
    """The browser's request body, streamed on to Pi as it arrives, under a byte cap.

    With `digest`, the last chunk waits until the whole body hashes to it, so Pi never receives a
    complete file other than the one named. A refusal stops the upload mid-way; Pi sees a cut-off
    request and saves nothing.
    """

    def __init__(self, request: Request, limit: int, too_large: AuthError, *, digest=None):
        self.request, self.limit, self.too_large, self.digest = request, limit, too_large, digest
        self.refusal: AuthError | None = None

    def refuse(self, error: AuthError) -> AuthError:
        self.refusal = error
        return error

    async def chunks(self):
        size, held, hasher = 0, b"", hashlib.sha256()
        async for chunk in self.request.stream():
            if not chunk:
                continue
            size += len(chunk)
            if size > self.limit:
                raise self.refuse(self.too_large)
            if self.digest is None:
                yield chunk
                continue
            hasher.update(chunk)
            if held:
                yield held
            held = chunk
        if self.digest is not None:
            if hasher.hexdigest() != self.digest:
                raise self.refuse(
                    AuthError(
                        "This is not the file you previewed. Preview it again, then import.",
                        409,
                        code="file_changed",
                    )
                )
            if held:
                yield held


def media_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "").split(";", 1)[0].strip().lower()


async def read_bounded(response: httpx.Response, limit: int) -> bytes:
    declared = response.headers.get("content-length")
    if declared is not None and (not declared.isdecimal() or int(declared) > limit):
        raise unreadable()
    raw = bytearray()
    async for chunk in response.aiter_bytes():
        raw.extend(chunk)
        if len(raw) > limit:
            raise unreadable()
    return bytes(raw)


async def json_answer(response: httpx.Response, limit: int) -> JSONResponse:
    if media_type(response) != "application/json":
        raise unreadable()
    try:
        value = json.loads(await read_bounded(response, limit))
    except (ValueError, UnicodeError, RecursionError):
        raise unreadable() from None
    if not isinstance(value, dict):
        raise unreadable()
    return JSONResponse(value, status_code=response.status_code)


def disposition(response: httpx.Response, extension: str) -> str:
    """Pi's attachment header, rebuilt from its checked parts: never inline, never a path."""
    found = DISPOSITION.fullmatch(response.headers.get("content-disposition", ""))
    if found is None or not found[1].lower().endswith("." + extension) or ".." in found[1]:
        raise unreadable()
    value = f'attachment; filename="{found[1]}"'
    if found[2] is not None:
        try:
            readable = unquote(found[2], errors="strict")
        except UnicodeError:
            raise unreadable() from None
        if (
            ".." in readable
            or any(c in readable for c in "/\\")
            or any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for c in readable)
        ):
            raise unreadable()
        value += "; filename*=UTF-8''" + found[2]
    return value


async def relay(
    transport: httpx.AsyncBaseTransport | None,
    method: str,
    url: str,
    headers: dict[str, str],
    *,
    read_seconds: float,
    params: dict[str, str] | None = None,
    upload: Upload | None = None,
    download: Download | None = None,
):
    """Send one request to Pi and answer the browser: the file for `download`, else Pi's JSON."""
    client = httpx.AsyncClient(
        transport=transport,
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(CONNECT_SECONDS, read=read_seconds, write=read_seconds),
    )
    try:
        outgoing = client.build_request(
            method,
            url,
            headers={**headers, "Accept-Encoding": "identity"},
            params=params,
            content=upload.chunks() if upload is not None else None,
        )
        response = await client.send(outgoing, stream=True)
    except Exception:
        await client.aclose()
        if upload is not None and upload.refusal is not None:
            raise upload.refusal from None
        raise unavailable() from None

    async def close():
        await response.aclose()
        await client.aclose()

    streaming = False
    try:
        if 300 <= response.status_code < 400:
            raise AuthError("Pi returned an unexpected redirect. Check gateway service URLs.", 502)
        # Asked for identity, so the bytes read below are the bytes Pi sent.
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise unreadable()
        if response.status_code != 200:
            # Pi's refusal (its {"detail": {"code", "message"}} shape) is the answer.
            return await json_answer(response, MAX_ERROR_BYTES)
        if download is None:
            return await json_answer(response, MAX_JSON_BYTES)
        if media_type(response) != download.media:
            if download.other_type and media_type(response):
                raise AuthError(download.other_type, 422, code="unsupported_format")
            raise unreadable()
        declared = response.headers.get("content-length", "")
        if not declared.isdecimal():
            raise unreadable()
        length = int(declared)
        if length > download.max_bytes:
            raise AuthError(
                f"This file is larger than {download.max_bytes // (1024 * 1024)} MB, "
                "the most the browser can download.",
                413,
                code="too_large",
            )
        named = disposition(response, download.extension)
        streaming = True
    finally:
        if not streaming:
            await close()

    async def body():
        sent = 0
        try:
            async for chunk in response.aiter_bytes():
                sent += len(chunk)
                if sent > length:
                    return  # more than Pi declared: stop, and the browser sees a failed download
                yield chunk
        except httpx.HTTPError:
            return  # a short file fails in the browser against its Content-Length
        finally:
            await close()

    return StreamingResponse(
        body(),
        media_type=download.content_type,
        headers={
            "Content-Disposition": named,
            "Content-Length": str(length),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(close),
    )
