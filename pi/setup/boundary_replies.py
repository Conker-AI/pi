"""The reply of `GET /setup/boundaries`, described in OpenAPI (D-01).

Pi passes ToolGate's policy summary (`GET /v2/agent/policy-summary`) through as ToolGate sent it,
after checking its digest (pi.actions.toolgate.ToolGateClient.policy_summary). The route describes
it with `pi.core.models.described` and does not re-validate the reply.
"""

from pydantic import JsonValue

from pi.core.models import Strict


class ToolGatePolicyTool(Strict):
    """One active tool Pi may call. `usageLimits` holds the tool policy's own limits that are set
    (max_per_minute, max_per_hour, cooldown_seconds, max_runtime_seconds, max_steps), unchecked."""

    id: str
    name: str
    authorization: str
    executionType: str
    usageLimits: dict[str, JsonValue]
    definitionDigest: str


class ToolGatePolicySummary(Strict):
    """`digest` is the SHA-256 of the other three fields, canonical JSON."""

    lockdown: bool
    scopePatterns: list[str]
    tools: list[ToolGatePolicyTool]
    digest: str
