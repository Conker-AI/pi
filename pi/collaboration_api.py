"""Admin-only preparation routes. Gateway wiring is deliberately separate."""
from fastapi import APIRouter, Depends

from . import agents, collaboration as c


def create_router(store_provider, authorize):
    router = APIRouter(prefix="/collaboration", dependencies=[Depends(authorize)])

    @router.get("")
    def list_configurations():
        return c.list_all(store_provider())

    @router.post("/templates")
    def create_template(body: c.Template):
        return c.save(store_provider(), "template", body)

    @router.get("/templates/{identity}")
    def get_template(identity: str):
        return c.get(store_provider(), identity, "template")

    @router.post("/templates/{identity}/update")
    def update_template(identity: str, body: c.UpdateTemplate):
        return c.save(store_provider(), "template", body.definition, identity, body.expected_revision)

    @router.post("/templates/{identity}/publish")
    def publish_template(identity: str, body: c.Revision):
        return c.publish(store_provider(), identity, body)

    @router.post("/templates/{identity}/instantiate")
    def instantiate_template(identity: str, body: c.Instantiate):
        return c.prepare(store_provider(), identity, "template", body)

    @router.post("/templates/{identity}/archive")
    def archive_template(identity: str, body: agents.ArchiveAgent):
        return c.archive(store_provider(), identity, "template", body)

    @router.post("/templates/{identity}/remove")
    def remove_template(identity: str, body: c.Revision):
        return c.remove(store_provider(), identity, "template", body)

    @router.post("/teams")
    def create_team(body: c.Team):
        return c.save(store_provider(), "team", body)

    @router.get("/teams/{identity}")
    def get_team(identity: str):
        return c.get(store_provider(), identity, "team")

    @router.post("/teams/{identity}/update")
    def update_team(identity: str, body: c.UpdateTeam):
        return c.save(store_provider(), "team", body.definition, identity, body.expected_revision)

    @router.post("/teams/{identity}/prepare")
    def prepare_team(identity: str, body: c.Revision):
        return c.prepare(store_provider(), identity, "team", body)

    @router.post("/teams/{identity}/archive")
    def archive_team(identity: str, body: agents.ArchiveAgent):
        return c.archive(store_provider(), identity, "team", body)

    @router.post("/teams/{identity}/remove")
    def remove_team(identity: str, body: c.Revision):
        return c.remove(store_provider(), identity, "team", body)

    return router
