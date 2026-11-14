"""Prompt enhance: Jev rewrites an unsent draft for the owner; nothing is saved or sent."""

from fastapi import APIRouter, Depends

from pi.agents import prompt_enhance
from pi.core.models import described


def router(get_store, get_providers, authorize):
    routes = APIRouter(prefix="/prompts", dependencies=[Depends(authorize)])

    @routes.post("/enhance", responses=described(prompt_enhance.EnhancedPrompt))
    def enhance(body: prompt_enhance.Request):
        """A clearer, better organised version of the draft, with notes on what changed."""
        return prompt_enhance.enhance(get_store(), get_providers(), body)

    return routes
