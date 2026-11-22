"""The exact, bounded read-only Codex history pagination contract."""

import re
from urllib.parse import parse_qsl, urlencode

THREAD_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CURSOR = r"[A-Za-z0-9_-]{1,8192}"


def parse_query(raw: bytes) -> dict:
    # A browser may percent-encode each cursor byte once. Decode within this
    # bound, then forward only the canonical <=8300-byte query to the host.
    if not isinstance(raw, bytes) or len(raw) > 25000:
        raise ValueError("Invalid native session pagination.")
    text = raw.decode("ascii")
    if re.search(r"%(?![0-9A-Fa-f]{2})", text):
        raise ValueError("Invalid native session pagination.")
    pairs = parse_qsl(
        text, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=3
    )
    values = dict(pairs)
    if len(pairs) != len(values) or set(values) - {"limit", "archived", "cursor"}:
        raise ValueError("Invalid native session pagination.")
    limit = values.get("limit", "25")
    archived = values.get("archived", "false")
    cursor = values.get("cursor")
    if (
        not re.fullmatch(r"[1-9][0-9]?", limit)
        or not 1 <= int(limit) <= 50
        or archived not in {"true", "false"}
        or (cursor is not None and not re.fullmatch(CURSOR, cursor))
    ):
        raise ValueError("Invalid native session pagination.")
    return {
        "limit": int(limit),
        "archived": archived == "true",
        **({"cursor": cursor} if cursor else {}),
    }


def canonical_query(raw: bytes) -> bytes:
    value = parse_query(raw)
    value["archived"] = "true" if value["archived"] else "false"
    return urlencode(value).encode("ascii")
