"""Owner-reviewed skill import and authenticated read-only discovery."""

from fastapi import APIRouter, Depends

from pi.agents import skills


def router(store, read, write):
    api = APIRouter(prefix="/skills")

    @api.get("", dependencies=[Depends(read)])
    def list_skills():
        return skills.list_skills(store())

    @api.get("/{identity}", dependencies=[Depends(read)])
    def get_skill(identity: str):
        return skills.get(store(), identity)

    @api.post("/import", dependencies=[Depends(write)])
    def import_skill(body: skills.Import):
        return skills.import_skill(store(), body)

    @api.post("/{identity}/state", dependencies=[Depends(write)])
    def state(identity: str, body: skills.State):
        return skills.set_state(store(), identity, body)

    return api
