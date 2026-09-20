"""Optional, public build assets. Never expose a filesystem browser or owner state."""

from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, Response

API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
UI_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "font-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
    "media-src 'self' blob:; object-src 'none'; frame-src 'none'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)

# Only public build formats. In particular: no source maps, HTML applications,
# databases, configuration/credential files, Python, or arbitrary unknown files.
MEDIA_TYPES = {
    ".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
    ".otf": "font/otf", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".avif": "image/avif", ".ico": "image/vnd.microsoft.icon", ".svg": "image/svg+xml",
    ".txt": "text/plain", ".diff": "text/plain", ".webmanifest": "application/manifest+json",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".mp4": "video/mp4", ".webm": "video/webm",
}
RESERVED = {"api", "auth", "health", "docs", "redoc", "openapi.json"}
STATIC_NAMESPACES = {"assets", "fonts", "images", "fixtures"}
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILES = 4096


def _safe_parts(path: str) -> bool:
    return (
        not any(character in path for character in ("\\", ":", "%", "\x00"))
        and not any(ord(character) < 32 or ord(character) == 127 for character in path)
        and (not path or all(part and not part.startswith(".") for part in path.split("/")))
    )


def _accepts_html(accept: str) -> bool:
    for item in accept.split(","):
        media, *parameters = item.strip().lower().split(";")
        if media != "text/html":
            continue
        try:
            quality = next(
                (float(p.strip()[2:]) for p in parameters if p.strip().startswith("q=")), 1
            )
        except ValueError:
            return False
        if quality > 0:
            return True
    return False


class DashboardAssets:
    """Snapshot an immutable dist directory once; HTTP requests never read disk.

    The directory is a trusted deployment artifact, not an upload location.
    Symlinks and junction escapes fail startup; later disk changes cannot replace
    the vetted bytes. Bound memory use rather than retain unbounded build output.
    """

    def __init__(self, directory: str):
        supplied = Path(directory)
        if not supplied.is_absolute() or not supplied.is_dir() or supplied.is_symlink():
            raise ValueError("GATEWAY_DASHBOARD_DIR must name an existing absolute dist directory.")
        root = supplied.resolve(strict=True)
        index = root / "index.html"
        if not index.is_file() or index.is_symlink() or index.resolve() != index:
            raise ValueError("GATEWAY_DASHBOARD_DIR requires a regular index.html inside dist.")
        self.files: dict[str, tuple[bytes, str]] = {}
        self.namespaces = set(STATIC_NAMESPACES)
        total = 0
        inspected = 0
        for path in root.rglob("*"):
            inspected += 1
            if inspected > MAX_FILES:
                raise ValueError("Dashboard dist exceeds the 4096-entry limit.")
            resolved = path.resolve(strict=True)
            if path.is_symlink() or not resolved.is_relative_to(root) or resolved != path:
                raise ValueError("Dashboard dist must not contain symlinks or path aliases.")
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if not _safe_parts(relative) or relative.split("/")[0].lower() in RESERVED:
                continue
            media_type = (
                "text/html" if relative == "index.html" else MEDIA_TYPES.get(path.suffix.lower())
            )
            if media_type is None:
                continue
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("A dashboard asset exceeds the 16 MiB limit.")
            with path.open("rb") as stream:
                content = stream.read(MAX_FILE_BYTES + 1)
            total += len(content)
            if len(content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise ValueError(
                    "Dashboard assets exceed their 16 MiB file or 128 MiB total limit."
                )
            self.files[relative] = (content, media_type)
            if "/" in relative:
                self.namespaces.add(relative.split("/")[0])
        if "index.html" not in self.files:
            raise ValueError("Dashboard dist has no readable index.html.")

    def response(self, path: str, request: Request) -> Response:
        path = path.removesuffix("/")
        first = path.split("/")[0]
        if not _safe_parts(path) or first.lower() in RESERVED:
            return JSONResponse({"detail": "Not found."}, status_code=404)
        asset = self.files.get(path)
        if asset is None:
            # Only extensionless document navigation reaches the SPA. Missing
            # assets never become successful HTML responses, even with Accept: */*.
            if (first in self.namespaces or "." in path
                    or not _accepts_html(request.headers.get("accept", ""))):
                return JSONResponse({"detail": "Not found."}, status_code=404)
            asset = self.files["index.html"]
        content, media_type = asset
        request.state.dashboard_response = True
        return Response(
            content=b"" if request.method == "HEAD" else content,
            media_type=media_type,
            headers={"Content-Length": str(len(content))},
        )
