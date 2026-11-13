"""Owner-reviewed skill import and authenticated read-only discovery."""

from fastapi import APIRouter, Depends

from pi.agents import skills
from pi.agents.skill_replies import SkillDetail, SkillList
from pi.core.models import described


def router(store, read, write):
    api = APIRouter(prefix="/skills")
    detail = described(SkillDetail)

    @api.get("", dependencies=[Depends(read)], responses=described(SkillList))
    def list_skills():
        return skills.list_skills(store())

    @api.get("/{identity}", dependencies=[Depends(read)], responses=detail)
    def get_skill(identity: str):
        return skills.get(store(), identity)

    @api.post("/import", dependencies=[Depends(write)], responses=detail)
    def import_skill(body: skills.Import):
        return skills.import_skill(store(), body)

    @api.post("/{identity}/state", dependencies=[Depends(write)], responses=detail)
    def state(identity: str, body: skills.State):
        return skills.set_state(store(), identity, body)

    return api
