"""Native and office-format file exports of one artifact version."""

from __future__ import annotations

import csv
import io
import json
import re

from pi.content import office_exports
from pi.content.artifact_models import NativeExport
from pi.content.artifacts import ArtifactError, get


def export(store, identity, version=None, resolve=None, *, format="native"):
    view = get(store, identity, resolve)
    if view["availability"] not in ("available", "source-archived"):
        raise ArtifactError("source_unavailable", "Source privacy or availability blocks export.")
    if version is not None and (type(version) is not int or version < 1):
        raise ArtifactError("invalid_version", "Choose an existing version.", 422)
    selected = (
        view["versions"][-1]
        if version is None
        else next((v for v in view["versions"] if v["version"] == version), None)
    )
    if selected is None:
        raise ArtifactError("invalid_version", "Choose an existing version.", 422)
    data, mime, extension = selected["content"], "text/plain;charset=utf-8", "txt"
    kind = data["kind"]
    plain, readable = office_exports.names(selected["title"])
    if format != "native":
        if not office_exports.supports(kind, format):
            raise ArtifactError(
                "unsupported_format", "Choose a format supported by this artifact.", 422
            )
        suffix = f"-v{selected['version']}.{format}"
        return {
            "filename": plain + suffix,
            "readable": readable + suffix,
            "mime": office_exports.MIME[format],
            "content": office_exports.render(
                data,
                format,
                title=selected["title"],
                citations=selected.get("citations") if kind == "markdown" else None,
            ),
        }
    if kind == "table":
        output = io.StringIO(newline="")
        writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
        for row in [data["columns"], *data["rows"]]:
            writer.writerow(
                [
                    "'" + cell
                    if cell.startswith(("\t", "\r", "\n"))
                    or re.match(r"^[\s\x00-\x1f]*[=+@-]", cell)
                    else cell
                    for cell in row
                ]
            )
        text, mime, extension = (
            output.getvalue().removesuffix("\r\n"),
            "text/csv;charset=utf-8",
            "csv",
        )
    elif kind in ("chart", "diagram", "media"):
        text, mime, extension = (
            json.dumps(data, ensure_ascii=False, indent=2),
            "application/json;charset=utf-8",
            "json",
        )
    else:
        text = data["text"]
        if kind == "markdown" and selected.get("citations"):
            appendix = json.dumps(selected["citations"], ensure_ascii=False, indent=2).replace(
                "`", "\\u0060"
            )
            text += (
                "\n\n## Supplied source references\n\n"
                "These references were supplied with the source response; "
                "they were not independently verified.\n\n```json\n" + appendix + "\n```\n"
            )
        extensions = {
            "javascript": "js",
            "js": "js",
            "typescript": "ts",
            "ts": "ts",
            "jsx": "jsx",
            "tsx": "tsx",
            "python": "py",
            "py": "py",
            "json": "json",
            "css": "css",
            "sql": "sql",
            "bash": "sh",
            "yaml": "yaml",
            "markdown": "md",
            "html": "html.txt",
            "svg": "svg.txt",
            "xml": "xml.txt",
        }
        extension = (
            "html.txt"
            if kind == "html"
            else "md"
            if kind == "markdown"
            else extensions.get(data["language"].lower(), "txt")
        )
    return NativeExport(
        artifactId=identity,
        version=selected["version"],
        filename=f"{plain}-v{selected['version']}.{extension}",
        mime=mime,
        text=text,
        privateOrigin=view["privateOrigin"] is True,
    ).model_dump()
