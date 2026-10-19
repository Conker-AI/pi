"""Write the OpenAPI documents in docs/ from the code, or check that they still match it.

Run from the repository root: python scripts/export_openapi.py [--check]
docs/pi-openapi.json and docs/gateway-openapi.json are the API reference the README links to.
Written by hand once, they fell months behind the routes; now CI fails when they differ.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway.api import app as gateway_app
from pi.api.server import app as pi_app

DOCUMENTS = {Path("docs/pi-openapi.json"): pi_app, Path("docs/gateway-openapi.json"): gateway_app}


def render(app) -> str:
    return json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    check = "--check" in sys.argv[1:]
    stale = []
    for path, app in DOCUMENTS.items():
        text = render(app)
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        if check:
            stale.append(path)
        else:
            path.write_bytes(text.encode("utf-8"))
            print(f"wrote {path}")
    for path in stale:
        print(f"{path} differs from the code; run python scripts/export_openapi.py")
    return 1 if stale else 0


if __name__ == "__main__":
    sys.exit(main())
