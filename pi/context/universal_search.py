"""Owner search over explicit public projections, never execution or raw event payloads.

Nothing from a forgotten or trashed chat is listed (`pi.conversation.visibility`): not the chat,
its tasks, activity or artifacts, the memories cited from it or saved from its messages, nor an
agent or AI job Conker kept from it. Restoring the chat brings them back.
"""

import hashlib
import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query

from pi.agents import profiles as agents
from pi.content import artifacts
from pi.context import projects
from pi.context.search_ranking import ranking_configuration, rerank
from pi.context.search_replies import SearchCapabilities, SearchPage
from pi.context.search_settings import (
    SOURCES,
    SaveSettings,
    SearchSettingsDocument,
    load_settings,
    save_settings,
)
from pi.conversation import hidden_records as shared_hidden
from pi.conversation import search as conversation_search
from pi.conversation import settings as session_settings
from pi.conversation import visibility
from pi.conversation.search_excerpt import excerpt
from pi.core.models import described
from pi.models.providers import ProviderUnavailable
from pi.schedule import jobs, tasks

SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_search_settings (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,
 configuration TEXT NOT NULL
);
"""
SCAN_LIMIT = 200


def public_session(store, identity):
    """A chat whose records may appear in search: not private, not forgotten, not in the trash."""
    with store._connect() as db:
        privacy = session_settings.source_privacy(db, identity)
        if visibility.hidden(db, [identity]):
            return False
    return privacy is not None and not any(privacy.values())


def hidden_records(db, agent_ids=()):
    """(kind, id) of each MemoryGate record from a hidden chat: the set recall leaves out
    (pi.conversation.hidden_records). The library has no own-chat exception."""
    found = shared_hidden.hidden_records(db, agent_ids)
    return {(kind, identity) for kind, ids in found.items() for identity in ids}


def visible_memories(store, items, agent_ids=()):
    """`items` without the memories of a hidden chat: by `hidden_records`, or a citation of the
    chat or of one of its messages. Malformed citations hide nothing, as in recall."""
    return shared_hidden.filter_cited(store, {"memories": list(items)}, agent_ids)["memories"]


def artifact_chats(row):
    """The chats an artifact listing names: the message it was copied from, and its task's chat."""
    source, task = row.get("source") or {}, row.get("task") or {}
    return {source.get("sessionId"), task.get("originSessionId")} - {None}


def kept_from_hidden(store, kind):
    with store._connect() as db:
        return visibility.kept_from_hidden(db, kind)


def record(source, identity, title, text, href, query, stage="metadata", role=None):
    haystack = text if stage == "text" else title + "\n" + text
    if query.casefold() not in haystack.casefold():
        return None
    return {
        "id": f"{source}:{identity}:{stage}",
        "source": source,
        "recordId": identity,
        "title": title[:500],
        "excerpt": excerpt(text, query),
        "href": href,
        "matchType": stage,
        "role": role,
    }


def conversation_metadata(store, query):
    """Bound eligible matches, rather than the recent chats considered for search."""
    results = []
    folded = query.casefold()
    with store._connect() as db:
        db.execute("BEGIN")
        db.create_function(
            "search_metadata_match",
            2,
            lambda title, summary: folded in (title + "\n" + (summary or "")).casefold(),
            deterministic=True,
        )
        rows = db.execute(
            f"SELECT id,title,summary FROM sessions WHERE NOT {visibility.hidden_sql('sessions.id')} "
            "AND search_metadata_match(title,summary) "
            "ORDER BY created_at DESC,id DESC"
        )
        # Stream matches on this same read snapshot. The shared privacy policy
        # includes current settings, legacy defaults, and immutable private origin.
        # Private candidates never consume the bounded public result allowance.
        for row in rows:
            privacy = session_settings.source_privacy(db, row["id"])
            if privacy is None or any(privacy.values()):
                continue
            hit = record(
                "conversations",
                row["id"],
                row["title"],
                row["summary"] or "",
                f"/chat?session={quote(row['id'])}",
                query,
            )
            if hit:
                results.append(hit)
                if len(results) > SCAN_LIMIT:
                    return results[:SCAN_LIMIT], True
    return results, False


def lexical(store, memory, toolgate, query, settings, stage):
    results, coverage = [], []

    def add(source, identity, title, text, href, role=None):
        hit = record(source, identity, title, text, href, query, stage, role)
        if hit:
            results.append(hit)

    for source in settings["sources"]:
        partial = False
        try:
            if source == "conversations":
                if stage == "text":
                    cursor = None
                    for _ in range(SCAN_LIMIT // 50):
                        page = conversation_search.search(store, query, limit=50, cursor=cursor)
                        for row in page["results"]:
                            # The source matched full retained text after privacy
                            # checks. A bounded preview may contain only part of
                            # a long match; it must not veto that verified result.
                            results.append(
                                {
                                    "id": f"{source}:{row['id']}:{stage}",
                                    "source": source,
                                    "recordId": row["id"],
                                    "title": row["title"][:500],
                                    "excerpt": excerpt(row["excerpt"], query),
                                    "href": f"/chat?session={quote(row['session_id'])}&message={quote(row['id'])}",
                                    "matchType": stage,
                                    "role": row["role"],
                                }
                            )
                        cursor = page["next_cursor"]
                        if cursor is None:
                            break
                    partial = cursor is not None
                else:
                    matches, partial = conversation_metadata(store, query)
                    results.extend(matches)
            elif source == "projects":
                rows = projects.list_projects(store, limit=SCAN_LIMIT + 1)
                partial = len(rows) > SCAN_LIMIT
                for row in rows[:SCAN_LIMIT]:
                    add(
                        source,
                        row["id"],
                        row["name"],
                        row["instructions"] if stage == "text" else row["description"],
                        f"/projects/{row['id']}",
                    )
            elif source == "artifacts":
                rows = artifacts.list_artifacts(
                    store, session_settings.source_privacy, limit=SCAN_LIMIT + 1
                )
                partial = len(rows) > SCAN_LIMIT or stage == "text"
                rows = rows[:SCAN_LIMIT]
                with store._connect() as db:
                    gone = visibility.hidden(
                        db, {chat for row in rows for chat in artifact_chats(row)}
                    )
                for row in rows:
                    if (
                        row["availability"] not in {"available", "source-archived"}
                        or row["privateOrigin"]
                        or artifact_chats(row) & gone
                    ):
                        continue
                    text = ""
                    if stage == "text":
                        item = artifacts.get(store, row["id"], session_settings.source_privacy)
                        if (
                            item["availability"] not in {"available", "source-archived"}
                            or item["privateOrigin"]
                        ):
                            continue
                        body = item["versions"][-1].get("content", {}) if item["versions"] else {}
                        if body.get("kind") in {"markdown", "code"}:
                            text = body.get("text", "")
                    add(source, row["id"], row["title"], text, f"/artifacts/{row['id']}")
            elif source == "tasks":
                page = tasks.list_tasks(store, limit=SCAN_LIMIT)
                partial = page["next_cursor"] is not None
                for row in page["results"]:
                    if row["content_status"] == "available" and public_session(
                        store, row["session_id"]
                    ):
                        text = row["status_note"] or ""
                        if stage == "text":
                            text += (
                                "\n"
                                + row["outcome"]
                                + "\n"
                                + "\n".join(c.get("text", "") for c in row["criteria"])
                            )
                        add(
                            source,
                            row["id"],
                            row["outcome"],
                            text,
                            f"/activity?tab=tasks&task={row['id']}",
                        )
            elif source == "agents":
                rows = agents.list_agents(store)["results"]
                partial = len(rows) > SCAN_LIMIT
                kept = kept_from_hidden(store, "agent")
                for row in rows[:SCAN_LIMIT]:
                    if row["id"] in kept:
                        continue
                    cfg = row["configuration"]
                    add(
                        source,
                        row["id"],
                        cfg["name"],
                        cfg.get("instructions", "") if stage == "text" else cfg.get("role", ""),
                        "/settings/companion?tab=harness"
                        if row["id"] == "companion"
                        else f"/agents/{row['id']}/edit",
                    )
            elif source == "jobs":
                rows = jobs.list_jobs(store, limit=SCAN_LIMIT + 1)
                partial = len(rows) > SCAN_LIMIT
                kept = kept_from_hidden(store, "ai_job")
                for row in rows[:SCAN_LIMIT]:
                    if row["id"] in kept:
                        continue
                    cfg = jobs.browser_job(row).model_dump()["definition"]
                    add(
                        source,
                        row["id"],
                        cfg["name"],
                        f"{cfg['state']} {cfg['timeZone']}",
                        f"/jobs/{row['id']}",
                    )
            elif source == "tools":
                if toolgate is None:
                    raise ValueError("unavailable")
                rows = toolgate.tools()
                partial = len(rows) > SCAN_LIMIT
                for row in rows[:SCAN_LIMIT]:
                    add(
                        source,
                        row.id,
                        row.name,
                        row.description,
                        f"/tools?tool={quote(row.id, safe='')}",
                    )
            elif source == "activity":
                with store._connect() as db:
                    rows = db.execute(
                        "SELECT e.id,e.kind,e.session_id,e.task_id,e.to_status FROM activity_events e "
                        "JOIN sessions s ON s.id=e.session_id "
                        f"WHERE NOT {visibility.hidden_sql('s.id')} "
                        "ORDER BY e.sequence DESC LIMIT ?",
                        (SCAN_LIMIT + 1,),
                    ).fetchall()
                partial = len(rows) > SCAN_LIMIT
                for row in rows[:SCAN_LIMIT]:
                    if public_session(store, row["session_id"]):
                        add(
                            source,
                            row["id"],
                            row["kind"].replace("_", " "),
                            row["to_status"] or "",
                            f"/activity?tab=events&event={quote(row['id'])}&sessionId={quote(row['session_id'])}",
                        )
            elif source == "memory":
                if memory.client is None:
                    raise ValueError("unavailable")
                page = memory.client.inspect(
                    "library", {"scope": "all", "search": query[:200], "limit": 50}
                )
                partial = page.get("next_after") is not None or stage == "text"
                with store._connect() as db:
                    gone = hidden_records(db, (getattr(memory.client, "agent_id", None),))
                for row in page["objects"]:
                    if (row.get("type"), row["id"]) in gone:
                        continue
                    add(
                        source,
                        row["id"],
                        row["title"],
                        row.get("preview", ""),
                        f"/memory?view=database&q={quote(query)}&record={quote(row['id'])}&kind={row['type']}",
                    )
            coverage.append({"source": source, "status": "partial" if partial else "searched"})
        except (ValueError, TypeError, KeyError, ProviderUnavailable, OSError):
            coverage.append({"source": source, "status": "unavailable"})
        except Exception:
            # Dependency exceptions are projected to a fixed status, never raw messages.
            coverage.append({"source": source, "status": "unavailable"})
    return results, coverage


def router(store, memory, toolgate, providers, authorize):
    routes = APIRouter(prefix="/search", dependencies=[Depends(authorize)])

    @routes.get("/settings", responses=described(SearchSettingsDocument))
    def settings():
        return load_settings(store())

    @routes.get("/capabilities", responses=described(SearchCapabilities))
    def capabilities():
        return {
            "sources": list(SOURCES),
            "exactText": True,
            "semanticSources": ["memory"] if memory().client is not None else [],
            "reranking": ranking_configuration(store()) is not None,
            "indexing": "source-owned",
            "scanLimit": SCAN_LIMIT,
        }

    @routes.post("/settings", responses=described(SearchSettingsDocument))
    def save(body: SaveSettings):
        return save_settings(
            store(),
            body,
            semantic_available=memory().client is not None,
            ranking_available=ranking_configuration(store()) is not None,
        )

    @routes.get("", responses=described(SearchPage))
    def search(
        q: str = Query(min_length=1, max_length=200),
        stage: str = Query(default="metadata", pattern="^(metadata|text|semantic)$"),
        cursor: str | None = Query(default=None, max_length=100),
        limit: int = Query(default=30, ge=1, le=50),
    ):
        query = q.strip()
        if not query:
            raise HTTPException(422, "Enter search text.")
        saved = load_settings(store())
        cfg = saved["configuration"]
        if (stage == "text" and not cfg["exactText"]) or (
            stage == "semantic" and not cfg["semantic"]
        ):
            return {
                "results": [],
                "coverage": [],
                "nextCursor": None,
                "stage": stage,
                "ranking": {"status": "disabled"},
            }
        digest = hashlib.sha256(json.dumps([query, stage, saved["revision"]]).encode()).hexdigest()[
            :16
        ]
        offset = 0
        if cursor:
            try:
                token, number = cursor.split(":")
                offset = int(number)
                if token != digest or not 0 <= offset <= 10000:
                    raise ValueError()
            except ValueError:
                raise HTTPException(422, "Search changed; restart pagination.") from None
        rank = {"status": "disabled"}
        if stage == "semantic":
            results, coverage = [], []
            if "memory" in cfg["sources"] and memory().client is None:
                coverage = [{"source": "memory", "status": "unavailable"}]
            if "memory" in cfg["sources"] and memory().client is not None:
                try:
                    package = memory().client.retrieve(query)
                    mode = package["retrieval"].get("mode")
                    semantic = mode in {"semantic", "hybrid", "vector"}
                    agent = getattr(memory().client, "agent_id", None)
                    for item in visible_memories(store(), package["memories"], (agent,)):
                        results.append(
                            {
                                "id": f"memory:{item['id']}:semantic",
                                "source": "memory",
                                "recordId": item["id"],
                                "title": (item.get("summary") or "Memory")[:500],
                                "excerpt": (item.get("text") or item.get("summary") or "")[:400],
                                "href": f"/memory?view=database&record={quote(item['id'])}&kind=memory",
                                "matchType": "semantic" if semantic else "text",
                                "role": None,
                            }
                        )
                    coverage = [
                        {"source": "memory", "status": "searched" if semantic else "degraded"}
                    ]
                except Exception:
                    coverage = [{"source": "memory", "status": "unavailable"}]
        else:
            results, coverage = lexical(store(), memory(), toolgate(), query, cfg, stage)
        # Stable source ordering precedes optional bounded relevance ranking.
        results.sort(
            key=lambda row: (SOURCES.index(row["source"]), row["title"].casefold(), row["id"])
        )
        # Rank within the literal page so variable model output cannot skip or repeat
        # records across pagination boundaries.
        page = results[offset : offset + limit]
        if cfg["reranking"]:
            page, rank = rerank(store(), providers(), query, page)
        return {
            "results": page,
            "coverage": coverage,
            "nextCursor": f"{digest}:{offset + limit}" if len(results) > offset + limit else None,
            "stage": stage,
            "ranking": rank,
        }

    return routes
