"""The owner's model settings (Conker's settings registry) that steer which model Pi asks.

- `models.mode_fast`, `models.mode_balanced`, `models.mode_deep`: the answer models a mode's Auto
  proposal may choose from, first choice first. Empty, or none of them usable, offers every
  answer model as before.
- `models.jobs.judge`, `models.jobs.fact_check`: the model that judges answers or checks facts
  when the request names none: the first usable one. Empty keeps the routing model.
- `models.think`: "on" or "off" decides Auto's thinking choice; "auto" leaves it to the routing
  model.
- `models.complexity`: how hard the owner's requests usually are ("low", "medium", "high"),
  given to the routing model as an owner hint; "auto" sends nothing.

Each value is checked again here: a model id that is not an enabled answer model (the set
`roles.dispatch` accepts) is skipped, and anything else invalid counts as unset, which is today's
behaviour.
"""

from __future__ import annotations

from pi.core import owner_settings as owner_settings

MODE_MODELS = {
    "fast": "models.mode_fast",
    "balanced": "models.mode_balanced",
    "deep": "models.mode_deep",
}
JUDGE = "models.jobs.judge"
FACT_CHECK = "models.jobs.fact_check"
THINK = "models.think"
COMPLEXITY = "models.complexity"
COMPLEXITIES = ("low", "medium", "high")
MAX_MODELS = 20


def usable(configuration: dict) -> list[str]:
    """The answer models a call may name: enabled, eligible for the answer role and on an enabled
    provider, in the answer role's order."""
    providers = {p["id"] for p in configuration["providers"] if p["enabled"]}
    models = {m["id"]: m for m in configuration["models"] if m["enabled"]}
    eligible = configuration["roleSettings"]["roles"]["answer"]["eligibleModelIds"]
    return [
        i for i in dict.fromkeys(eligible) if i in models and models[i]["providerId"] in providers
    ]


def preferred(store, configuration: dict, setting_id: str) -> list[str]:
    """The owner's list for `setting_id`, kept to usable answer models, first choice first."""
    value = owner_settings.value(store, setting_id)
    if not isinstance(value, list):
        return []
    allowed = set(usable(configuration))
    chosen = [item for item in value[:MAX_MODELS] if isinstance(item, str) and item in allowed]
    return list(dict.fromkeys(chosen))


def first(store, configuration: dict, setting_id: str) -> str | None:
    """The owner's first usable model for `setting_id`, or None to keep today's choice."""
    chosen = preferred(store, configuration, setting_id)
    return chosen[0] if chosen else None


def think(store) -> bool | None:
    """True or False when the owner decided thinking for Auto; None leaves it to the router."""
    return {"on": True, "off": False}.get(owner_settings.value(store, THINK))


def complexity(store) -> str | None:
    """How hard the owner's requests usually are, or None when he left it on Auto."""
    value = owner_settings.value(store, COMPLEXITY)
    return value if value in COMPLEXITIES else None


def routing_hints(store, hints: dict | None) -> dict | None:
    """The turn's own owner hints plus his usual complexity, for the answer's routing model."""
    usual = complexity(store)
    if usual is None:
        return hints
    return {**(hints or {}), "usualComplexity": usual}
