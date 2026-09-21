"""Literal owner search over retained public conversation text, with exact IDs."""

from fastapi import APIRouter, Depends, HTTPException, Query


def search(store, query, limit=30, cursor=None):
    query = query.strip()
    if not 1 <= len(query) <= 500 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Use 1-500 search characters and a limit of 1-50.")
    pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    params = [query, pattern]
    after = ""
    with store._connect() as db:
        if cursor is not None:
            boundary = db.execute(
                "SELECT created_at,id FROM messages WHERE id=?", (cursor,)
            ).fetchone()
            if boundary is None:
                raise ValueError("Search cursor unavailable; restart search.")
            after = " AND (m.created_at,m.id)<(?,?)"
            params.extend(boundary)
        params.append(limit + 1)
        rows = db.execute(
            r"""
            SELECT m.id,m.session_id,m.seq,m.role,m.created_at,s.title,
                   substr(json_extract(m.content,'$'),
                       max(1,instr(lower(json_extract(m.content,'$')),lower(?))-100),500) AS excerpt
            FROM messages m JOIN sessions s ON s.id=m.session_id
            WHERE s.status!='forgotten' AND m.role IN ('user','assistant')
              AND json_valid(m.content) AND json_type(m.content)='text'
              AND json_extract(m.content,'$') LIKE ? ESCAPE '\'
              AND NOT EXISTS(SELECT 1 FROM session_settings settings
                  WHERE settings.session_id=s.id AND (
                    json_extract(settings.settings,'$.privacy.memoryDisabled')=1 OR
                    json_extract(settings.settings,'$.privacy.harnessDisabled')=1))
              AND NOT EXISTS(SELECT 1 FROM message_privacy p JOIN messages original
                  ON original.id=p.message_id WHERE original.session_id=s.id
                  AND (p.memory_disabled=1 OR p.harness_disabled=1))
        """
            + after
            + " ORDER BY m.created_at DESC,m.id DESC LIMIT ?",
            params,
        ).fetchall()
        return {
            "scope": "public-conversation-text",
            "results": [dict(row) for row in rows[:limit]],
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        }


def router(store, authorize):
    routes = APIRouter(prefix="/search", dependencies=[Depends(authorize)])

    @routes.get("/conversations")
    def conversations(
        query: str = Query(min_length=1, max_length=500),
        limit: int = Query(default=30, ge=1, le=50),
        cursor: str | None = Query(default=None, max_length=200),
    ):
        try:
            return search(store(), query, limit, cursor)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return routes
