"""Compact owner-readable chat directory; distinct from cross-source search.

Private chats remain readable by their owner, as on the existing session list.
This reads session metadata only, never transcripts, memory, or a model. Pages
are individual snapshots, not a claim that an earlier page is still current.
"""

import hashlib
import json
import math
import re

from pi.schedule.tasks import TaskError

MAX_QUERY = 200
MAX_CURSOR = 196
MAX_TITLE = 300
MAX_PREVIEW = 400
MAX_PAGE_BYTES = 256 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)
_CURSOR = re.compile(r"h1\.([0-9a-f]{64})\.([A-Za-z0-9_-]{1,128})", re.ASCII)
_FORGOTTEN = "(s.status='forgotten' OR f.session_id IS NOT NULL)"


def _invalid_request():
    raise TaskError("invalid_history_request", "Use a bounded history query and page size.", 422)


def _invalid_cursor():
    raise TaskError(
        "invalid_history_cursor", "History cursor unavailable; restart this search.", 422
    )


def _unavailable():
    raise TaskError("history_unavailable", "Conversation history is unavailable.", 503)


def request_arguments(params):
    """Validate the whole query, including unknown or repeated fields."""
    if set(params) - {"q", "limit", "cursor"} or any(
        len(params.getlist(key)) != 1 for key in params
    ):
        _invalid_request()
    raw_limit = params.get("limit", "50")
    if not re.fullmatch(r"[0-9]{1,2}", raw_limit, re.ASCII):
        _invalid_request()
    return {"limit": int(raw_limit), "q": params.get("q"), "cursor": params.get("cursor")}


def _arguments(limit, q, cursor):
    if type(limit) is not int or not 1 <= limit <= 50:
        _invalid_request()
    if q is not None and (not isinstance(q, str) or len(q) > MAX_QUERY or "\x00" in q):
        _invalid_request()
    try:
        normalized = (q or "").strip().casefold()
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    except UnicodeError:
        _invalid_request()
    anchor = None
    if cursor is not None:
        if not isinstance(cursor, str) or len(cursor) > MAX_CURSOR:
            _invalid_cursor()
        match = _CURSOR.fullmatch(cursor)
        if match is None or match[1] != digest:
            _invalid_cursor()
        anchor = match[2]
    return normalized, digest, anchor


def _text(value, maximum):
    return value[:maximum] if isinstance(value, str) else None


def _time(value, nullable=False):
    if value is None and nullable:
        return None
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 <= value <= 8_640_000_000_000
    ):
        _unavailable()
    return value


def _record(row):
    if (
        not isinstance(row["id"], str)
        or not _ID.fullmatch(row["id"])
        or (
            row["parent_id"] is not None
            and (not isinstance(row["parent_id"], str) or not _ID.fullmatch(row["parent_id"]))
        )
    ):
        _unavailable()
    if row["status"] not in {"open", "forked", "closed", "forgotten"} or not isinstance(
        row["title"], str
    ):
        _unavailable()
    return {
        "id": row["id"],
        "parent_id": row["parent_id"],
        "title": row["title"],
        "status": row["status"],
        "created_at": _time(row["created_at"]),
        "closed_at": _time(row["closed_at"], nullable=True),
        "summary_preview": row["summary_preview"],
        "title_truncated": bool(row["title_truncated"]),
        "summary_truncated": bool(row["summary_truncated"]),
        "content_status": "forgotten" if row["status"] == "forgotten" else "available",
    }


def page(store, *, limit=50, q=None, cursor=None):
    normalized, digest, anchor = _arguments(limit, q, cursor)
    with store._connect() as db:
        db.execute("BEGIN")
        # Python casefold handles Unicode; SQLite LOWER/NOCASE handles only ASCII.
        db.create_function(
            "history_casefold",
            1,
            lambda value: value.casefold() if isinstance(value, str) else "",
            deterministic=True,
        )
        # Unlike SQLite substr/length, these also preserve embedded NUL in old metadata.
        db.create_function("history_preview", 2, _text, deterministic=True)
        db.create_function(
            "history_length",
            1,
            lambda value: len(value) if isinstance(value, str) else 0,
            deterministic=True,
        )
        where, values = [], []
        if normalized:
            where.append(f"NOT {_FORGOTTEN}")
            # instr is a literal substring operation: %, _ and backslash are not patterns.
            where.append(
                "(instr(history_casefold(s.title),?)>0 OR instr(history_casefold(s.summary),?)>0)"
            )
            values.extend((normalized, normalized))
        if anchor is not None:
            boundary = db.execute(
                "SELECT created_at,id FROM sessions WHERE id=?", (anchor,)
            ).fetchone()
            if boundary is None:
                _invalid_cursor()
            _time(boundary["created_at"])
            where.append("(s.created_at,s.id)<(?,?)")
            values.extend((boundary["created_at"], boundary["id"]))
        clause = " WHERE " + " AND ".join(where) if where else ""
        rows = db.execute(
            f"SELECT s.id,s.parent_id,s.created_at,s.closed_at,"
            f"CASE WHEN {_FORGOTTEN} THEN 'forgotten' ELSE s.status END AS status,"
            f"CASE WHEN {_FORGOTTEN} THEN '' ELSE history_preview(s.title,{MAX_TITLE}) END AS title,"
            f"CASE WHEN {_FORGOTTEN} THEN NULL ELSE history_preview(s.summary,{MAX_PREVIEW}) END AS summary_preview,"
            f"CASE WHEN {_FORGOTTEN} THEN 0 ELSE history_length(s.title)>{MAX_TITLE} END AS title_truncated,"
            f"CASE WHEN {_FORGOTTEN} THEN 0 ELSE history_length(s.summary)>{MAX_PREVIEW} END AS summary_truncated "
            "FROM sessions s LEFT JOIN forgotten_sessions f ON f.session_id=s.id"
            + clause
            + " ORDER BY s.created_at DESC,s.id DESC LIMIT ?",
            (*values, limit + 1),
        ).fetchall()
        results = [_record(row) for row in rows[:limit]]
        result = {
            "schemaVersion": 1,
            "results": results,
            "next_cursor": f"h1.{digest}.{results[-1]['id']}" if len(rows) > limit else None,
        }
        if (
            len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            > MAX_PAGE_BYTES
        ):
            _unavailable()
        return result
