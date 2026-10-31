"""Office and PDF files from inert artifact or answer content, made in a short-lived worker.

Nothing is fetched and nothing is written to disk: the content goes to the worker on stdin and
the file comes back on stdout. Markdown keeps its structure (headings, lists, tables, code,
quotes, links); only http, https and mailto links stay clickable.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import unicodedata
from pathlib import Path
from urllib.parse import quote

from pi.core.errors import ServiceError

TIMEOUT = 40
WORKERS = threading.BoundedSemaphore(2)
# Must equal _export_worker.MAX_INPUT: the worker runs as a separate isolated script and
# keeps its own constant.
MAX_INPUT = 2 * 1024 * 1024
MAX_OUTPUT = 16 * 1024 * 1024
MIME = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
}
MAGIC = {"pdf": b"%PDF-", "docx": b"PK\x03\x04", "xlsx": b"PK\x03\x04", "pptx": b"PK\x03\x04"}
SUPPORTED = {
    "markdown": ("docx", "pdf", "pptx", "xlsx"),
    "table": ("docx", "pdf", "pptx", "xlsx"),
    "code": ("docx", "pdf", "pptx"),
}
REASONS = {
    "too_long": "This is too long to export: the limit is 400,000 characters.",
    "too_deep": "Lists or quotes sit more than 12 levels inside each other. Flatten them to export.",
    "too_many_blocks": "This has more than 10,000 paragraphs, headings and other blocks to export.",
    "table_too_big": (
        "A table is too big to export: at most 64 columns, 2,000 rows, 200 tables, 40,000 cells "
        "in all and 32,000 characters in a cell."
    ),
    "page_cells": "Tables over 10,000 cells export only as XLSX.",
    "no_table": "There is no table here to put in a spreadsheet. Choose DOCX or PDF.",
    "pages": "The PDF would be longer than 500 pages.",
    "slides": "The slides would run past 300 slides.",
    "too_big": "The file would be larger than 16 MB.",
    "layout": "A table or block is too large for a PDF page. Export it as DOCX or XLSX instead.",
    "control": "The text holds control characters that a document cannot store.",
    "invalid": "The export could not be made from this content.",
}


class ExportError(ServiceError):
    pass


def supports(kind: str, format: str) -> bool:
    return format in SUPPORTED.get(kind, ())


def _check(value):
    """Office files are XML: refuse what XML cannot hold rather than drop it silently."""
    if isinstance(value, str):
        if any(
            (ord(c) < 32 and c not in "\t\n\r") or 0xD800 <= ord(c) <= 0xDFFF or c in "\ufffe\uffff"
            for c in value
        ):
            raise ExportError("export_control", REASONS["control"], 422)
    elif isinstance(value, dict | list):
        for item in value.values() if isinstance(value, dict) else value:
            _check(item)


def names(title: str) -> tuple[str, str]:
    """A safe file name: ASCII for every client, and the readable one for clients that take it."""
    plain = re.sub(r"[^a-zA-Z0-9_-]+", "-", unicodedata.normalize("NFKD", title))
    plain = plain.strip("-")[:80].strip("-") or "artifact"
    readable = re.sub(r"[^\w-]+", "-", unicodedata.normalize("NFC", title))
    readable = readable.strip("-")[:80].strip("-") or plain
    if any(re.fullmatch(r"con|prn|aux|nul|com[0-9]|lpt[0-9]", n, re.I) for n in (plain, readable)):
        return "artifact-" + plain, "artifact-" + readable
    return plain, readable


def disposition(ascii_name: str, readable: str) -> str:
    value = f'attachment; filename="{ascii_name}"'
    if readable != ascii_name:
        value += "; filename*=UTF-8''" + quote(readable, safe="")
    return value


def render(data: dict, format: str, *, title: str, citations=None) -> bytes:
    if not supports(data.get("kind", ""), format):
        raise ExportError("unsupported_format", "Choose a format supported by this artifact.", 422)
    request = {"format": format, "title": title, "content": data, "citations": citations or []}
    _check(request)
    payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_INPUT:
        raise ExportError("export_too_long", REASONS["too_long"], 422)
    if not WORKERS.acquire(timeout=5):
        raise ExportError(
            "export_busy", "Two exports are already running. Try again in a moment.", 503
        )
    try:
        # Isolated Python ignores user site packages and PYTHON* settings; the content goes
        # through stdin only, never the command line, the disk or diagnostics.
        result = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).with_name("_export_worker.py"))],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=TIMEOUT,
            env={
                key: os.environ[key]
                for key in ("SYSTEMROOT", "WINDIR", "PATH")
                if key in os.environ
            },
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise ExportError(
            "export_unavailable",
            "The export took too long or could not start. Try again, or export a shorter part.",
            503,
        ) from None
    finally:
        WORKERS.release()
    output = result.stdout
    if result.returncode == 0 and output.startswith(b"{") and len(output) < 1024:
        try:
            code = json.loads(output)["error"]
        except (ValueError, KeyError, TypeError):
            code = "invalid"
        code = code if code in REASONS else "invalid"
        raise ExportError("export_" + code, REASONS[code], 422)
    if result.returncode or not output.startswith(MAGIC[format]) or len(output) > MAX_OUTPUT:
        raise ExportError(
            "export_unavailable",
            "The export took too long or could not start. Try again, or export a shorter part.",
            503,
        )
    return output
