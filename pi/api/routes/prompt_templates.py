"""Prompt templates: the owner's list of forms, with starters (R-F8). Owner session, no password:
a template grants nothing, it only shapes the next form and the wand's fill."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from pi.agents import prompt_templates
from pi.core.models import described


def router(store, require_owner):
    routes = APIRouter(prefix="/prompts/templates", dependencies=[Depends(require_owner)])

    @routes.get("", responses=described(prompt_templates.TemplatePage))
    def list_templates():
        """Every template: the starters first, then the owner's own, newest first."""
        return prompt_templates.listing(store())

    @routes.get("/{template_id}", responses=described(prompt_templates.TemplateReceipt))
    def read_template(template_id: str):
        """One template with its fields."""
        return {"template": prompt_templates.get(store(), template_id)}

    @routes.post("", responses=described(prompt_templates.TemplateReceipt))
    def create_template(body: prompt_templates.TemplateBody):
        """Save a new template (at most 100)."""
        return prompt_templates.create(store(), body)

    @routes.post("/{template_id}", responses=described(prompt_templates.TemplateReceipt))
    def update_template(template_id: str, body: prompt_templates.TemplateUpdate):
        """Replace a template's name, description and fields; `expected_revision` must match."""
        return prompt_templates.update(store(), template_id, body)

    @routes.post("/{template_id}/delete", responses=described(prompt_templates.TemplateDeleted))
    def delete_template(template_id: str, body: prompt_templates.TemplateDelete):
        """Delete a template, a starter too: a deleted starter does not come back."""
        return prompt_templates.delete(store(), template_id, body)

    return routes
