"""Standalone export worker. Reads one bounded JSON request; writes the file or a fixed code.

Parsing and layout run here, in a short-lived process with CPU, memory and time limits, so a
pathological answer (thousands of brackets, a huge table) can slow only this process.
"""

import json
import logging
import sys
from pathlib import Path

MAX_INPUT = 2 * 1024 * 1024
MAX_OUTPUT = 16 * 1024 * 1024
MAGIC = {"pdf": b"%PDF-", "docx": b"PK\x03\x04", "xlsx": b"PK\x03\x04", "pptx": b"PK\x03\x04"}


def blocks(request):
    from pi.content import markdown_blocks as m

    data = request["content"]
    if data["kind"] == "markdown":
        result = m.from_markdown(data["text"])
    elif data["kind"] == "table":
        result = m.from_table(data["columns"], data["rows"])
    elif data["kind"] == "code":
        result = [m.Block("code", text=data["text"], language=data.get("language", ""))]
    else:
        raise ValueError("unsupported")
    return result + m.citation_blocks(request.get("citations") or [])


def render(request):
    from pi.content import export_documents, export_pdf, export_slides
    from pi.content.markdown_blocks import page_cells

    title, chosen = request["title"], blocks(request)
    if request["format"] != "xlsx":
        page_cells(chosen)
    match request["format"]:
        case "docx":
            return export_documents.docx(chosen, title)
        case "xlsx":
            sheet = "Data" if request["content"]["kind"] == "table" else None
            return export_documents.xlsx(chosen, title, sheet)
        case "pdf":
            return export_pdf.pdf(chosen, title)
        case "pptx":
            return export_slides.pptx(chosen, title)
    raise ValueError("unsupported")


def main():
    logging.disable(logging.CRITICAL)
    # Isolated Python adds no source folder to the path; only this checkout's own package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from pi.content.markdown_blocks import ExportLimit

    try:
        if sys.platform.startswith("linux"):
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        request = json.loads(raw) if len(raw) <= MAX_INPUT else None
        if not isinstance(request, dict) or request.get("format") not in MAGIC:
            result = {"error": "invalid"}
        else:
            content = render(request)
            if len(content) > MAX_OUTPUT:
                result = {"error": "too_big"}
            elif not content.startswith(MAGIC[request["format"]]):
                result = {"error": "invalid"}
            else:
                sys.stdout.buffer.write(content)
                return
    except ExportLimit as exc:
        result = {"error": exc.code}
    except (MemoryError, RecursionError):
        result = {"error": "too_big"}
    except Exception:
        result = {"error": "invalid"}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode("ascii"))


if __name__ == "__main__":
    main()
