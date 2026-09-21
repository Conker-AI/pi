"""Standalone parser worker. Reads bounded stdin; emits only text or a fixed code."""

import io
import json
import logging
import sys


def extract(raw, maximum):
    from pypdf import PdfReader, filters

    # Parser limits supplement process limits, including on Windows where RLIMIT
    # is unavailable. They are not an OS memory sandbox.
    for key in ("ZLIB_MAX_OUTPUT_LENGTH", "LZW_MAX_OUTPUT_LENGTH", "RUN_LENGTH_MAX_OUTPUT_LENGTH",
                "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH", "MAX_DECLARED_STREAM_LENGTH"):
        setattr(filters, key, 8 * 1024 * 1024)
    reader = PdfReader(io.BytesIO(raw), strict=True, root_object_recovery_limit=1000)
    if reader.is_encrypted:
        return {"error": "encrypted"}
    if not 1 <= len(reader.pages) <= 200:
        return {"error": "limit"}
    parts, count, content_bytes, found = [], 0, 0, False
    for number, page in enumerate(reader.pages, 1):
        stream = page.get_contents()
        content_bytes += len(stream.get_data()) if stream is not None else 0
        if content_bytes > 16 * 1024 * 1024:
            return {"error": "limit"}
        text = page.extract_text() or ""
        found = found or bool(text.strip())
        part = f"[PDF page {number}]\n" + (text.strip() or "[No extractable text; images and scans require OCR.]")
        count += len(part) + (2 if parts else 0)
        if count > maximum:
            return {"error": "limit"}
        parts.append(part)
    return {"text": "\n\n".join(parts)} if found else {"error": "no_text"}


def main():
    logging.disable(logging.CRITICAL)
    try:
        if sys.platform.startswith("linux"):
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        maximum = int(sys.argv[1])
        raw = sys.stdin.buffer.read(10 * 1024 * 1024 + 1)
        if not 1 <= maximum <= 200_000 or len(raw) > 10 * 1024 * 1024 or not raw.startswith(b"%PDF-"):
            result = {"error": "limit"}
        else:
            result = extract(raw, maximum)
    except MemoryError:
        result = {"error": "limit"}
    except Exception:
        result = {"error": "invalid"}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode("ascii"))


if __name__ == "__main__":
    main()
