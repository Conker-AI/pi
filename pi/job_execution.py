"""Scheduled effects use server-provisioned, scoped ToolGate clients only."""

from __future__ import annotations

import time

import httpx

from .jobs import Target


class PublishedJobs:
    def __init__(self, clients, *, transport=None):
        # Agent names in job definitions never select credentials from the request.
        self.clients = dict(clients)
        self.transport = transport

    def __call__(
        self, target, *, action_id, agent_id, approval_request_id=None, spending_job_id=None
    ):
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
        if spending_job_id is not None:
            payload["job_id"] = spending_job_id
        if approval_request_id is not None:
            payload["approval_request_id"] = approval_request_id
        try:
            response = self._request(
                client,
                "POST",
                path,
                json=payload,
            )
        except (httpx.HTTPError, ValueError):
            return self.unknown()
        return self.outcome(response, target, action_id)

    def reconcile(self, target, *, action_id, agent_id):
        """Read receipts only. A missing receipt is not permission to repeat."""
        target = Target.model_validate(target)
        client = self.clients.get(agent_id)
        if client is None or not client.execution_key:
            return self.unknown()
        try:
            response = self._request(
                client,
                "GET",
                f"/v2/agent/actions/{action_id}",
            )
        except (httpx.HTTPError, ValueError):
            return self.unknown()
        if response.status_code != 200:
            return self.unknown()
        return self.outcome(response, target, action_id)

    def validate_budget(self, budget_id, *, action_id, agent_id):
        client = self.clients.get(agent_id)
        if client is None or not client.execution_key:
            return False
        try:
            response = self._request(client, "GET", f"/v2/agent/spending/jobs/{budget_id}")
            body = response.json()
            return (
                response.status_code == 200
                and isinstance(body, dict)
                and body.get("job_id") == budget_id
                and body.get("root_action_id") == action_id
                and type(body.get("cap")) is int
                and body["cap"] > 0
            )
        except (httpx.HTTPError, ValueError):
            return False

    def allocate_budget(self, allowance_id, *, target, action_id, agent_id):
        target = Target.model_validate(target)
        client = self.clients.get(agent_id)
        if client is None or not client.execution_key:
            raise ValueError("Budget authority unavailable")
        response = self._request(client, "POST",
            f"/v2/agent/spending/allowances/{allowance_id}/allocate",
            json={"root_action_id": action_id, "target": target.model_dump()})
        value = response.json()
        if (response.status_code != 200 or not isinstance(value, dict)
                or value.get("root_action_id") != action_id
                or type(value.get("cap")) is not int or value["cap"] <= 0):
            raise ValueError("Budget allocation unavailable")
        return value.get("job_id")

    def _request(self, gate, method, path, **kwargs):
        # Receipts are metadata; never buffer arbitrary tool output or inherit
        # proxy credentials/configuration from the worker's environment.
        deadline = time.monotonic() + gate.timeout
        with (
            httpx.Client(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=gate.timeout,
            ) as client,
            client.stream(
                method,
                gate.base_url + path,
                headers={**gate._headers(), "Accept-Encoding": "identity"},
                **kwargs,
            ) as response,
        ):
            if response.headers.get("content-encoding", "identity") != "identity":
                raise ValueError("Encoded receipt is unsupported")
            raw = bytearray()
            for chunk in response.iter_raw():
                if len(raw) + len(chunk) > 256 * 1024 or time.monotonic() > deadline:
                    raise ValueError("Receipt limit exceeded")
                raw.extend(chunk)
            if time.monotonic() > deadline:
                raise ValueError("Receipt deadline exceeded")
            return httpx.Response(response.status_code, content=bytes(raw))

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
