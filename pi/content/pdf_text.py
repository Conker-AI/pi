"""PDF text extraction in a short-lived worker, reusing attachment provenance."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

TIMEOUT = 10
WORKERS = threading.BoundedSemaphore(2)
REASONS = {
    "encrypted": "Password-protected PDFs are not supported; upload an unlocked copy.",
    "no_text": "No PDF text layer found. Scanned pages require OCR, which is not connected.",
    "limit": "PDF exceeds the page, text or extraction resource limit.",
    "invalid": "PDF text could not be extracted. The file may be damaged or unsupported.",
    "unavailable": "PDF extraction is unavailable or timed out. Try again or upload text.",
}


class PDFError(ValueError):
    pass


def extract(raw, max_characters):
    if (
        type(raw) is not bytes
        or not raw.startswith(b"%PDF-")
        or len(raw) > 10 * 1024 * 1024
        or type(max_characters) is not int
        or not 1 <= max_characters <= 200_000
    ):
        raise PDFError(REASONS["invalid"])
    if not WORKERS.acquire(timeout=1):
        raise PDFError(REASONS["unavailable"])
    try:
        # No attachment content is placed on disk, the command line or diagnostics.
        # Isolated Python ignores user site packages and PYTHON* environment settings.
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                str(Path(__file__).with_name("_pdf_worker.py")),
                str(max_characters),
            ],
            input=raw,
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
        if result.returncode or len(result.stdout) > max_characters * 6 + 1024:
            raise PDFError(REASONS["unavailable"])
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise PDFError(REASONS["invalid"])
        if "error" in value:
            raise PDFError(REASONS.get(value["error"], REASONS["invalid"]))
        text = value.get("text")
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > max_characters
            or any((ord(c) < 32 and c not in "\t\r\n") or ord(c) == 127 for c in text)
        ):
            raise PDFError(REASONS["invalid"])
        return text
    except PDFError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        raise PDFError(REASONS["unavailable"]) from None
    finally:
        WORKERS.release()
