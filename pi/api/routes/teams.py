"""Owner team definitions; preparation and template operations stay recovery-only."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

from pi.agents import profiles as agents
from pi.agents import team_models
from pi.agents import teams as c


def create_router(store_provider, authorize, owner_authorize=None):
    router = APIRouter(prefix="/collaboration", dependencies=[Depends(authorize)])

    # Separate routers keep recovery-only operations behind their original authorization.
    def no_store(response: Response):
        response.headers["Cache-Control"] = "no-store"

    owner = APIRouter(
        prefix="/collaboration",
        dependencies=[Depends(owner_authorize or authorize), Depends(no_store)],
    )
    TeamPath = Annotated[str, Path(pattern=r"^team_[0-9a-f]{32}$")]

    def owner_call(function, *args, **kwargs):
        try:
            return function(store_provider(), *args, **kwargs)
        except ValueError as exc:
            raise HTTPException(503, "Stored team configuration is unavailable.") from exc

    @router.get("")
    def list_configurations():
        return c.list_all(store_provider())

    @router.post("/templates")
    def create_template(body: team_models.Template):
        return c.save(store_provider(), "template", body)

    @router.get("/templates/{identity}")
    def get_template(identity: str):
        return c.get(store_provider(), identity, "template")

    @router.post("/templates/{identity}/update")
    def update_template(identity: str, body: team_models.UpdateTemplate):
        return c.save(
            store_provider(), "template", body.definition, identity, body.expected_revision
        )

    @router.post("/templates/{identity}/publish")
    def publish_template(identity: str, body: team_models.Revision):
        return c.publish(store_provider(), identity, body)

    @router.post("/templates/{identity}/instantiate")
    def instantiate_template(identity: str, body: team_models.Instantiate):
        return c.prepare(store_provider(), identity, "template", body)

    @router.post("/templates/{identity}/archive")
    def archive_template(identity: str, body: agents.ArchiveAgent):
        return c.archive(store_provider(), identity, "template", body)

    @router.post("/templates/{identity}/remove")
    def remove_template(identity: str, body: team_models.Revision):
        return c.remove(store_provider(), identity, "template", body)

    @owner.get("/teams", response_model=team_models.TeamCollection)
    def list_teams(
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = Query(default=None, pattern=r"^team_[0-9a-f]{32}$"),
    ):
        return owner_call(c.owner_list, limit, cursor)

    @owner.post("/teams", response_model=team_models.TeamView)
    def create_team(body: team_models.Team):
        return owner_call(c.save, "team", body, owner_view=True)

    @owner.get("/teams/{identity}", response_model=team_models.TeamView)
    def get_team(identity: TeamPath):
        return owner_call(c.owner_get, identity)

    @owner.post("/teams/{identity}/update", response_model=team_models.TeamView)
    def update_team(identity: TeamPath, body: team_models.UpdateTeam):
        return owner_call(
            c.save, "team", body.definition, identity, body.expected_revision, owner_view=True
        )

    @owner.get("/teams/{identity}/versions", response_model=team_models.TeamHistory)
    def team_versions(
        identity: TeamPath,
        limit: int = Query(default=50, ge=1, le=100),
        after: int = Query(default=0, ge=0, le=2147483647),
    ):
        return owner_call(c.owner_history, identity, limit, after)

    @owner.get("/teams/{identity}/versions/{revision}", response_model=team_models.TeamRevision)
    def team_revision(identity: TeamPath, revision: int = Path(ge=1, le=2147483647)):
        return owner_call(c.owner_revision, identity, revision)

    @router.post("/teams/{identity}/prepare")
    def prepare_team(identity: str, body: team_models.Revision):
        return c.prepare(store_provider(), identity, "team", body)

    @owner.post("/teams/{identity}/archive", response_model=team_models.TeamView)
    def archive_team(identity: TeamPath, body: agents.ArchiveAgent):
        return owner_call(c.archive, identity, "team", body, owner_view=True)

    @owner.post("/teams/{identity}/restore", response_model=team_models.TeamView)
    def restore_team(identity: TeamPath, body: team_models.Revision):
        return owner_call(
            c.archive,
            identity,
            "team",
            agents.ArchiveAgent(expected_revision=body.expected_revision, archived=False),
            owner_view=True,
        )

    @router.post("/teams/{identity}/remove")
    def remove_team(identity: str, body: team_models.Revision):
        return c.remove(store_provider(), identity, "team", body)

    combined = APIRouter()
    combined.include_router(router)
    combined.include_router(owner)
    return combined
