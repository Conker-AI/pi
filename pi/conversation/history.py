"""Compact owner-readable chat directory; distinct from cross-source search.

Private chats remain readable by their owner, as on the existing session list.
This reads session metadata only, never transcripts, memory, or a model. Pages
are individual snapshots, not a claim that an earlier page is still current.
"""

import hashlib
import json
import math
import re
from typing import Any, NoReturn, cast

from pi.conversation import visibility
from pi.schedule.tasks import TaskError

MAX_QUERY = 200
MAX_CURSOR = 196
MAX_TITLE = 300
MAX_PREVIEW = 400
MAX_PAGE_BYTES = 256 * 1024
# `ids` names at most this many chats, so one lookup is one page.
MAX_IDS = 50
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}", re.ASCII)
_CURSOR = re.compile(r"h1\.([0-9a-f]{64})\.([A-Za-z0-9_-]{1,128})", re.ASCII)
_FORGOTTEN = "(s.status='forgotten' OR f.session_id IS NOT NULL)"
# Archived chats are hidden by default; the owner can list only them, or both.
ARCHIVED = {"exclude": "a.session_id IS NULL", "only": "a.session_id IS NOT NULL", "include": None}
# Trashed chats are hidden from every list; the trash view lists them whatever their archive state.
TRASHED = {"exclude": "t.session_id IS NULL", "only": "t.session_id IS NOT NULL"}
# Each order is a key tuple and a direction; the id breaks ties so pages never overlap.
# Name order puts untitled chats last and compares names the way people read them (casefold).
_NAME = f"CASE WHEN {_FORGOTTEN} THEN '' ELSE history_casefold(s.title) END"
ORDERS = {
    "newest": (("s.created_at", "s.id"), "DESC"),
    "oldest": (("s.created_at", "s.id"), "ASC"),
    "name": ((f"({_NAME})=''", _NAME, "s.id"), "ASC"),
}


def _invalid_request() -> NoReturn:
    raise TaskError("invalid_history_request", "Use a bounded history query and page size.", 422)


def _invalid_cursor() -> NoReturn:
    raise TaskError(
        "invalid_history_cursor", "History cursor unavailable; restart this search.", 422
    )


def _unavailable() -> NoReturn:
    raise TaskError("history_unavailable", "Conversation history is unavailable.", 503)


def request_arguments(params):
    """Validate the whole query, including unknown or repeated fields."""
    if set(params) - {"q", "limit", "cursor", "archived", "trashed", "sort", "ids"} or any(
        len(params.getlist(key)) != 1 for key in params
    ):
        _invalid_request()
    raw_limit = params.get("limit", "50")
    if not re.fullmatch(r"[0-9]{1,2}", raw_limit, re.ASCII):
        _invalid_request()
    arguments = {
        "limit": int(raw_limit),
        "q": params.get("q"),
        "cursor": params.get("cursor"),
        "archived": params.get("archived", "exclude"),
        "trashed": params.get("trashed", "exclude"),
        "sort": params.get("sort", "newest"),
    }
    if "ids" in params:
        # Comma-separated; checked for size before it is split.
        if len(params["ids"]) > MAX_IDS * 129:
            _invalid_request()
        arguments["ids"] = params["ids"].split(",")
    return arguments


def _chat_ids(ids):
    """At most MAX_IDS distinct chat ids, or None when the caller named none."""
    if ids is None:
        return None
    if (
        not isinstance(ids, (list, tuple))
        or not 1 <= len(ids) <= MAX_IDS
        or not all(isinstance(value, str) and _ID.fullmatch(value) for value in ids)
    ):
        _invalid_request()
    return tuple(dict.fromkeys(ids))


def _arguments(limit, q, cursor, archived="exclude", sort="newest", trashed="exclude", ids=None):
    if type(limit) is not int or not 1 <= limit <= 50:
        _invalid_request()
    if archived not in ARCHIVED or trashed not in TRASHED or sort not in ORDERS:
        _invalid_request()
    ids = _chat_ids(ids)
    # Named chats are looked up only among the visible ones: the Trash view takes no ids.
    if ids is not None and trashed != "exclude":
        _invalid_request()
    if q is not None and (not isinstance(q, str) or len(q) > MAX_QUERY or "\x00" in q):
        _invalid_request()
    try:
        normalized = (q or "").strip().casefold()
        # A cursor belongs to one search, filter and order; the default view keeps its old digest.
        bound = (
            normalized
            if (archived, sort) == ("exclude", "newest")
            else (f"{archived}\x00{sort}\x00{normalized}")
        )
        if trashed != "exclude":
            bound += f"\x00{trashed}"
        if ids is not None:
            bound += "\x00ids\x00" + ",".join(sorted(ids))
        digest = hashlib.sha256(bound.encode("utf-8")).hexdigest()
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
    return normalized, digest, anchor, ids


def _text(value, maximum):
    return value[:maximum] if isinstance(value, str) else None


def _time(value: Any, nullable=False):
    if value is None and nullable:
        return None
    if (
        type(value) not in (int, float)
        or not math.isfinite(cast(float, value))
        or not 0 <= cast(float, value) <= 8_640_000_000_000
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
        "archived_at": _time(row["archived_at"], nullable=True),
        "trashed_at": _time(row["trashed_at"], nullable=True),
    }


def page(
    store,
    *,
    limit=50,
    q=None,
    cursor=None,
    archived="exclude",
    sort="newest",
    trashed="exclude",
    ids=None,
):
    """One page of the directory; `ids` narrows it to those chats that are still visible.

    With `ids`, a forgotten chat is left out instead of listed as a tombstone, so a client that
    remembers chats it opened (Home's "Jump back in") gets back only the ones it may still show.
    """
    normalized, digest, anchor, ids = _arguments(limit, q, cursor, archived, sort, trashed, ids)
    keys, direction = ORDERS[sort]
    key = "(" + ",".join(keys) + ")"
    joins = (
        " FROM sessions s LEFT JOIN forgotten_sessions f ON f.session_id=s.id"
        " LEFT JOIN session_archive a ON a.session_id=s.id"
        " LEFT JOIN session_trash t ON t.session_id=s.id"
    )
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
        where.append(TRASHED[trashed])
        if ids is not None:
            # Hidden covers forgotten, trashed and every chat made from one (visibility).
            where.append(f"NOT {visibility.hidden_sql('s.id')}")
            where.append(f"s.id IN ({','.join('?' * len(ids))})")
            values.extend(ids)
            if ARCHIVED[archived]:
                where.append(ARCHIVED[archived])
        elif trashed == "exclude":
            # A chat made from a hidden one (a fork of a trashed chat, a kept job's chat) is
            # hidden with it; forgotten chats stay listed as tombstones.
            where.append(f"({_FORGOTTEN} OR NOT {visibility.hidden_sql('s.id')})")
            if ARCHIVED[archived]:
                where.append(ARCHIVED[archived])
        if normalized:
            where.append(f"NOT {_FORGOTTEN}")
            # instr is a literal substring operation: %, _ and backslash are not patterns.
            where.append(
                "(instr(history_casefold(s.title),?)>0 OR instr(history_casefold(s.summary),?)>0)"
            )
            values.extend((normalized, normalized))
        if anchor is not None:
            boundary = db.execute(
                f"SELECT s.created_at,{','.join(keys)}{joins} WHERE s.id=?", (anchor,)
            ).fetchone()
            if boundary is None:
                _invalid_cursor()
            _time(boundary[0])
            where.append(f"{key}{'<' if direction == 'DESC' else '>'}({','.join('?' * len(keys))})")
            values.extend(tuple(boundary)[1:])
        clause = " WHERE " + " AND ".join(where) if where else ""
        rows = db.execute(
            f"SELECT s.id,s.parent_id,s.created_at,s.closed_at,"
            f"CASE WHEN {_FORGOTTEN} THEN 'forgotten' ELSE s.status END AS status,"
            f"CASE WHEN {_FORGOTTEN} THEN '' ELSE history_preview(s.title,{MAX_TITLE}) END AS title,"
            f"CASE WHEN {_FORGOTTEN} THEN NULL ELSE history_preview(s.summary,{MAX_PREVIEW}) END AS summary_preview,"
            f"CASE WHEN {_FORGOTTEN} THEN 0 ELSE history_length(s.title)>{MAX_TITLE} END AS title_truncated,"
            f"CASE WHEN {_FORGOTTEN} THEN 0 ELSE history_length(s.summary)>{MAX_PREVIEW} END AS summary_truncated,"
            "a.archived_at,t.trashed_at"
            + joins
            + clause
            + " ORDER BY "
            + ",".join(f"{part} {direction}" for part in keys)
            + " LIMIT ?",
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
