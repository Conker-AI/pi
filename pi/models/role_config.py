"""Model-role configuration: the saved settings' shape and validation.

Selection and dispatch live in pi.models.roles.
"""

from __future__ import annotations

from typing import Literal, cast

from pydantic import Field, model_validator

from pi.core.models import Strict

ROLES = (
    "answer",
    "thinking",
    "routing",
    "context-selection",
    "summarization",
    "memory-ranking",
    "search-ranking",
    "proposals",
    "drafting",
    "reviewer",
    "memory-sorting",
    "tool-text",
)


# Roles added after configurations were first saved. Older saved settings gain them
# disabled: a new background use of the owner's conversations is never switched on
# by an upgrade.
LATER_ROLES = (
    "memory-ranking",
    "proposals",
    "search-ranking",
    "thinking",
    "drafting",
    "reviewer",
    "memory-sorting",
    "tool-text",
)


# Roles Conker's services call (see service_inference.py). Until the owner picks a model for
# one, it uses the local model the services used before, so nothing leaves the server.
LOCAL_ROLES = ("memory-sorting", "tool-text")


# A cold local model on a small server can take minutes to answer its first prompt.
LOCAL_TIMEOUT_MS = 300_000


MAX_TIMEOUT_MS = 600_000


# Backup models a role tries in order after its own model fails.
MAX_BACKUPS = 8


def default_timeout_ms(role: str, *, local: bool = False) -> int:
    """The wait a role gets when nothing was saved; saved settings keep their own."""
    if role == "memory-ranking":
        return 2000
    return LOCAL_TIMEOUT_MS if local or role in LOCAL_ROLES else 120000


def tries(assignment: dict) -> int:
    """How many models one call of a role may try: its own, then each backup in order."""
    return 1 + (len(assignment["fallbackModelIds"]) if assignment["failure"] == "fallback" else 0)


class Provider(Strict):
    id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=160)
    enabled: bool


class Model(Strict):
    id: str = Field(min_length=1, max_length=200)
    providerId: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=160)
    route: str = Field(min_length=1, max_length=300)
    enabled: bool
    routingDescription: str = Field(default="", max_length=240)


class Assignment(Strict):
    enabled: bool
    eligibleModelIds: list[str] = Field(max_length=200)
    modelId: str | None
    timeoutMs: int = Field(ge=100, le=MAX_TIMEOUT_MS)
    failure: Literal["stop", "fallback"]
    fallbackModelIds: list[str] = Field(
        max_length=MAX_BACKUPS,
        description="Backup models tried in this order after the role's model fails.",
    )
    followAnswer: bool = Field(
        default=False,
        description="Auto: use the answer role's model and backups. Every save fills them in "
        "again, so a change of the answer model reaches this role.",
    )

    @model_validator(mode="before")
    @classmethod
    def one_backup(cls, value):
        # Settings saved before backup lists, and older clients, name one fallback model. An
        # older client that read a list and sends one fallback back means that one.
        if isinstance(value, dict) and "fallbackModelId" in value:
            value = dict(value)
            legacy = value.pop("fallbackModelId")
            value["fallbackModelIds"] = [] if legacy is None else [legacy]
        return value


class Roles(Strict):
    answerMode: Literal["manual", "router"]
    roles: dict[str, Assignment] = Field(
        description="One assignment for each model role: " + ", ".join(ROLES) + "."
    )


class Configuration(Strict):
    providers: list[Provider] = Field(max_length=100)
    models: list[Model] = Field(max_length=1000)
    defaultModelId: str | None
    roleSettings: Roles

    @model_validator(mode="before")
    @classmethod
    def migrate_roles(cls, value):
        if isinstance(value, dict) and isinstance(value.get("roleSettings"), dict):
            roles = value["roleSettings"].get("roles")
            missing = [
                role for role in LATER_ROLES if isinstance(roles, dict) and role not in roles
            ]
            if missing:
                value = {
                    **value,
                    "roleSettings": {
                        **value["roleSettings"],
                        "roles": {
                            **roles,
                            **{
                                role: dict(
                                    enabled=False,
                                    eligibleModelIds=[],
                                    modelId=None,
                                    timeoutMs=default_timeout_ms(role),
                                    failure="stop",
                                    fallbackModelIds=[],
                                )
                                for role in missing
                            },
                        },
                    },
                }
        return follow_answer(value)

    @model_validator(mode="after")
    def references(self):
        providers = {p.id: p for p in self.providers}
        known = {m.id: m for m in self.models}
        if len(providers) != len(self.providers) or len(known) != len(self.models):
            raise ValueError("Provider and model IDs must be unique.")
        if any(m.providerId not in providers for m in self.models):
            raise ValueError("Model provider does not exist.")
        enabled = {m.id for m in self.models if m.enabled and providers[m.providerId].enabled}
        if self.defaultModelId not in enabled and (self.defaultModelId is not None or enabled):
            raise ValueError("Choose an enabled default model.")
        if set(self.roleSettings.roles) != set(ROLES):
            raise ValueError("Configure all known model roles.")
        for item in self.roleSettings.roles.values():
            eligible = set(item.eligibleModelIds)
            if len(eligible) != len(item.eligibleModelIds) or not eligible.issubset(known):
                raise ValueError("Role eligibility requires unique known model IDs.")
            backups = item.fallbackModelIds
            if any(
                value is not None and value not in eligible for value in (item.modelId, *backups)
            ):
                raise ValueError("Primary and fallback must be explicitly eligible.")
            if len(set(backups)) != len(backups):
                raise ValueError("Each fallback model may be listed once.")
            if item.modelId in backups:
                raise ValueError("Fallback must differ from primary.")
            if item.failure == "stop" and backups:
                raise ValueError("Stop-on-failure cannot specify fallback.")
            if item.enabled and (
                item.modelId not in enabled
                or (item.failure == "fallback" and (not backups or not enabled.issuperset(backups)))
            ):
                raise ValueError("Enabled roles need enabled primary/fallback routes.")
        if self.roleSettings.roles["answer"].followAnswer:
            raise ValueError("The answer role cannot follow itself.")
        if (
            self.roleSettings.answerMode == "manual"
            and self.roleSettings.roles["answer"].failure != "stop"
        ):
            raise ValueError("Manual answer locks never fallback.")
        if self.roleSettings.answerMode == "router" and not all(
            self.roleSettings.roles[r].enabled for r in ("answer", "routing")
        ):
            raise ValueError("Router mode requires answer and routing roles.")
        return self


def follow_answer(value):
    """Fill each Auto role with the answer role's model and backups; it keeps its own timeout."""
    roles = value.get("roleSettings", {}).get("roles") if isinstance(value, dict) else None
    answer = roles.get("answer") if isinstance(roles, dict) else None
    if not isinstance(answer, dict):
        return value
    model, backups = answer.get("modelId"), answer.get("fallbackModelIds")
    if backups is None:  # saved before backup lists
        backups = [] if answer.get("fallbackModelId") is None else [answer["fallbackModelId"]]
    backups = list(backups)
    followed = {
        name: {
            **{key: item for key, item in role.items() if key != "fallbackModelId"},
            "modelId": model,
            "eligibleModelIds": [] if model is None else [model, *backups],
            "failure": answer.get("failure", "stop") if backups else "stop",
            "fallbackModelIds": backups,
        }
        for name, role in cast(dict, roles).items()
        if name != "answer" and isinstance(role, dict) and role.get("followAnswer") is True
    }
    if not followed:
        return value
    settings = {**value["roleSettings"], "roles": {**cast(dict, roles), **followed}}
    return {**value, "roleSettings": settings}


class ConfigurationDocument(Strict):
    """What GET and POST /models/configuration answer; no configuration before the first save."""

    revision: int
    configuration: Configuration | None


SCHEMA = """
CREATE TABLE IF NOT EXISTS model_role_settings (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,
 configuration TEXT NOT NULL
);
"""
