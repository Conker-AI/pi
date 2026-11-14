"""Every kind the owner organizes, in one summary shape; each kind keeps its own routes."""

from fastapi import APIRouter, Depends, Query

from pi.agents import definitions
from pi.context import projects


def router(store, authorize):
    routes = APIRouter(dependencies=[Depends(authorize)])

    @routes.get("/definitions", response_model=definitions.Collection)
    def listing(
        kind: definitions.Kind | None = None,
        archived: bool = False,
        search: str = Query(default="", max_length=200),
    ):
        """Agents, teams, templates, skills, cron jobs and projects. Reads only."""
        return definitions.summaries(
            store(),
            kind,
            archived=archived,
            search=search,
            list_projects=projects.list_projects,
        )

    return routes
