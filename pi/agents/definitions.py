"""One summary shape for every kind the owner organizes, so one list can show them all.

Agents, teams, team templates, skills, cron jobs and projects each keep their own detail and
write routes; this module only projects their saved records into the same fields. It reads,
never writes, and it includes nothing a kind's own list would not already show.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from pi.agents import profiles, skills
from pi.agents import teams as collaboration
from pi.context import projects
from pi.core.models import Strict
from pi.schedule import jobs

Kind = Literal["agent", "team", "template", "skill", "job", "project"]
KINDS: tuple[str, ...] = ("agent", "team", "template", "skill", "job", "project")
DESCRIPTION = 200


class Summary(Strict):
    kind: Kind
    id: str
    name: str
    description: str = Field(max_length=DESCRIPTION)
    revision: int
    state: Literal["active", "archived", "disabled", "draft", "published"]
    createdAt: float
    updatedAt: float | None
    archivedAt: float | None
    href: str


class Collection(Strict):
    schemaVersion: Literal[1] = 1
    results: list[Summary]


def _short(text) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= DESCRIPTION else text[: DESCRIPTION - 1] + "…"


def _agents(store):
    for agent in profiles.list_agents(store)["results"]:
        yield {
            "kind": "agent",
            "id": agent["id"],
            "name": agent["configuration"]["name"],
            "description": _short(agent["configuration"]["role"]),
            "revision": agent["revision"],
            "state": "archived" if agent["archived_at"] else "active",
            "createdAt": agent["created_at"],
            "updatedAt": agent["updated_at"],
            "archivedAt": agent["archived_at"],
            "href": f"/agents/{agent['id']}",
        }


def _collaboration(store):
    records = collaboration.list_all(store)
    with store._connect() as db:
        published = {
            row[0] for row in db.execute("SELECT DISTINCT template_id FROM template_publications")
        }
    for kind, key in (("template", "templates"), ("team", "teams")):
        for record in records[key]:
            definition = record["definition"]
            state = "archived" if record["archived_at"] else "active"
            if kind == "template" and state == "active":
                state = "published" if record["id"] in published else "draft"
            yield {
                "kind": kind,
                "id": record["id"],
                "name": definition["name"],
                "description": _short(definition.get("description") or definition.get("objective")),
                "revision": record["revision"],
                "state": state,
                "createdAt": record["created_at"],
                "updatedAt": record["updated_at"],
                "archivedAt": record["archived_at"],
                "href": f"/collaboration/{key}/{record['id']}",
            }


def _skills(store):
    for skill in skills.list_skills(store)["results"]:
        yield {
            "kind": "skill",
            "id": skill["id"],
            "name": skill["name"],
            "description": _short(skill["description"]),
            "revision": skill["revision"],
            "state": "active" if skill["enabled"] else "disabled",
            "createdAt": skill["createdAt"],
            "updatedAt": skill["updatedAt"],
            "archivedAt": None,
            "href": f"/skills/{skill['id']}",
        }


def _jobs(store):
    for job in jobs.list_jobs(store):
        definition = job["definition"]
        yield {
            "kind": "job",
            "id": job["id"],
            "name": definition["name"],
            # Instructions stay on the job's own view (/jobs), not in this shared list.
            "description": "",
            "revision": job["revision"],
            "state": "active" if definition["enabled"] else "disabled",
            "createdAt": job["created_at"],
            "updatedAt": job.get("updated_at"),
            "archivedAt": None,
            "href": f"/jobs/{job['id']}",
        }


def _projects(store):
    for project in projects.list_projects(store):
        yield {
            "kind": "project",
            "id": project["id"],
            "name": project["name"],
            "description": _short(project["description"]),
            "revision": project["revision"],
            "state": "archived" if project["archivedAt"] else "active",
            "createdAt": project["createdAt"],
            "updatedAt": project["updatedAt"],
            "archivedAt": project["archivedAt"],
            "href": f"/projects/{project['id']}",
        }


SOURCES = {
    "agent": _agents,
    "team": _collaboration,
    "template": _collaboration,
    "skill": _skills,
    "job": _jobs,
    "project": _projects,
}


def summaries(store, kind: str | None = None, *, archived: bool = False, search: str = "") -> dict:
    """Every saved definition, or one kind, in kind order then by name."""
    wanted = [kind] if kind else list(KINDS)
    readers = list(dict.fromkeys(SOURCES[name] for name in wanted))
    needle = search.casefold().strip()
    rows = [
        row
        for reader in readers
        for row in reader(store)
        if row["kind"] in wanted
        and (archived or row["state"] != "archived")
        and (not needle or needle in (row["name"] + " " + row["description"]).casefold())
    ]
    rows.sort(key=lambda row: (KINDS.index(row["kind"]), row["name"].casefold(), row["id"]))
    return Collection(results=rows).model_dump()
