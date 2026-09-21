"""Scheduled effects use server-provisioned, scoped ToolGate clients only."""

from __future__ import annotations

import httpx

from .jobs import Target


class PublishedJobs:
    def __init__(self, clients):
        # Agent names in job definitions never select credentials from the request.
        self.clients = dict(clients)

    def __call__(self, target, *, action_id, agent_id, approval_request_id=None):
        target = Target.model_validate(target)
        client = self.clients.get(agent_id)
        if client is None or not client.execution_key:
            return {"status": "failed", "reason": "No scoped execution credential for this agent."}
        path = (
            f"/v2/tools/{target.id}/invoke"
            if target.kind == "tool"
            else f"/v2/automations/{target.id}/run"
        )
        payload = {
            "action_id": action_id,
            "args": target.args,
            "published_version": target.publishedVersion,
            "expected_publication_digest": target.digest,
        }
        if approval_request_id is not None:
            payload["approval_request_id"] = approval_request_id
        try:
            response = httpx.post(
                client.base_url + path,
                json=payload,
                headers=client._headers(),
                timeout=client.timeout,
            )
        except httpx.HTTPError:
            return self.unknown()
        return self.outcome(response, target, action_id)

    def reconcile(self, target, *, action_id, agent_id):
        """Read receipts only. A missing receipt is not permission to repeat."""
        target = Target.model_validate(target)
        client = self.clients.get(agent_id)
        if client is None or not client.execution_key:
            return self.unknown()
        try:
            response = httpx.get(
                client.base_url + f"/v2/agent/actions/{action_id}",
                headers=client._headers(),
                timeout=client.timeout,
            )
        except httpx.HTTPError:
            return self.unknown()
        if response.status_code != 200:
            return self.unknown()
        return self.outcome(response, target, action_id)

    @staticmethod
    def unknown():
        return {
            "status": "outcome_unknown",
            "reason": "Reconcile the existing action; do not repeat it.",
        }

    @classmethod
    def outcome(cls, response, target, action_id):
        try:
            body = response.json()
        except ValueError:
            return cls.unknown()
        if not isinstance(body, dict):
            return cls.unknown()
        detail = body.get("detail", body)
        if not isinstance(detail, dict):
            return cls.unknown()
        code = detail.get("code")
        if code in {"OUTCOME_UNKNOWN", "IN_PROGRESS", "ACTION_CONFLICT"}:
            return cls.unknown()
        # Only explicit policy/input rejection proves no new effect occurred.
        if 400 <= response.status_code < 500 and code in {
            "POLICY_DENIED",
            "PUBLICATION_MISMATCH",
            "PUBLICATION_REQUIRED",
            "APPROVAL_INVALID",
            "LOCKED_DOWN",
            "AGENT_REVOKED",
            "VALIDATION_ERROR",
        }:
            return {"status": "failed", "code": code}
        if response.status_code != 200:
            return cls.unknown()
        if code == "CONFIRMATION_REQUIRED" and isinstance(body.get("request_id"), str):
            return {
                "status": "awaiting_approval",
                "request_id": body["request_id"],
                "action_id": action_id,
                "expires_at": body.get("expires_at"),
            }
        if (
            body.get("action_id") != action_id
            or body.get("definition_version") != target.publishedVersion
            or body.get("publication_digest") != target.digest
            or body.get("status") != "completed"
        ):
            return cls.unknown()
        if code == "OK":
            if target.kind == "tool" and (
                not isinstance(body.get("result"), dict) or body["result"].get("ok") is not True
            ):
                return cls.unknown()
            return {
                "status": "completed",
                "action_id": action_id,
                "publication_digest": target.digest,
                "definition_version": target.publishedVersion,
            }
        if (
            code == "TOOL_UNAVAILABLE"
            and target.kind == "tool"
            and isinstance(body.get("result"), dict)
            and body["result"].get("ok") is False
        ):
            return {"status": "failed", "action_id": action_id, "code": code}
        return cls.unknown()
