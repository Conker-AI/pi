"""Owner-authored agent profiles: list, create, read versions, update, archive."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from . import agents


def router(app_state, require_owner):
    routes = APIRouter()

    @routes.get("/agents", dependencies=[Depends(require_owner)], response_model=agents.AgentCollection)
    def list_agents():
        return agents.list_agents(app_state().store)

    @routes.post("/agents", dependencies=[Depends(require_owner)], response_model=agents.AgentProfile)
    def create_agent(body: agents.AgentInput):
        return agents.create(app_state().store, body)

    @routes.get(
        "/agents/{identity}", dependencies=[Depends(require_owner)], response_model=agents.AgentProfile
    )
    def get_agent(identity: str):
        return agents.get(app_state().store, identity)

    @routes.get(
        "/agents/{identity}/versions",
        dependencies=[Depends(require_owner)],
        response_model=agents.AgentCollection,
    )
    def agent_history(identity: str):
        return agents.history(app_state().store, identity)

    @routes.get(
        "/agents/{identity}/versions/{revision}",
        dependencies=[Depends(require_owner)],
        response_model=agents.AgentProfile,
    )
    def agent_version(identity: str, revision: int):
        return agents.get(app_state().store, identity, revision)

    @routes.post(
        "/agents/{identity}/update",
        dependencies=[Depends(require_owner)],
        response_model=agents.AgentProfile,
    )
    def update_agent(identity: str, body: agents.UpdateAgent):
        return agents.update(app_state().store, identity, body)

    @routes.post(
        "/agents/{identity}/archive",
        dependencies=[Depends(require_owner)],
        response_model=agents.AgentProfile,
    )
    def archive_agent(identity: str, body: agents.ArchiveAgent):
        return agents.archive(app_state().store, identity, body)

    return routes
