"""The reply of the owner's plan usage route (`GET /plans/usage`), described in OpenAPI (D-01).

It names what pi.owner.plan_alerts.overview already sends; the route describes it with
`pi.core.models.described` and does not re-validate the reply.
"""

from typing import Literal

from pi.core.models import Strict


class PlanUsageWindow(Strict):
    """The latest reading of one plan window, as a subscription runtime reported it."""

    provider: Literal["chatgpt", "claude"]
    window: str
    label: str
    usedPercent: int
    resetsAt: str | None
    checkedAt: float


class PlanAlertLink(Strict):
    kind: Literal["plan-alert"]
    alertId: str


class PlanAlertItem(Strict):
    """An open alert the owner has not dismissed; its two numbers come from the alert's facts."""

    kind: Literal["plan"]
    id: str
    title: str
    since: float
    usedPercent: int | None
    resetsAt: str | None
    source: PlanAlertLink


class PlanUsageOverview(Strict):
    windows: list[PlanUsageWindow]
    alerts: list[PlanAlertItem]
