"""Text-only adapter to the official native CLI; no borrowed OAuth tokens."""

import re

import httpx

from pi.models import inference_control
from pi.models.chatgpt import ChatGPTProvider
from pi.models.evidence import project_tool_evidence
from pi.models.private_transport import PrivateTransport
from pi.models.providers import Completion, ProviderUnavailable, require_images

# What the subscription runtime accepts as a model id (it passes the alias or id to the CLI).
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,299}")


def answering_model(value: dict, requested: str) -> str:
    """The model id that really answered. The runtime resolves an alias such as `haiku` to the
    id the CLI used (`actualModel`); a missing, empty or malformed value keeps the alias asked."""
    actual = value.get("actualModel")
    if isinstance(actual, str) and _MODEL_ID.fullmatch(actual):
        return actual
    return requested


class ClaudeCodeProvider(ChatGPTProvider):
    name = "claude-code"

    def complete_bounded(self, messages, *, model, timeout):
        require_images(self, messages)
        if any(m.role == "tool" for m in messages):
            messages = project_tool_evidence(messages)
        try:
            with (
                self.cancelable_request() as cancellation,
                self.client(min(timeout, 180) + 5) as client,
            ):
                result = client.post(
                    "http://conker-chatgpt/responses",
                    json={
                        "model": model,
                        "messages": [{"role": m.role, "content": m.content} for m in messages],
                        "timeout": min(max(timeout, 1), 180),
                        **cancellation,
                    },
                )
                inference_control.check()
                result.raise_for_status()
                if len(result.content) > 2_000_000:
                    raise ValueError()
                value = result.json()
                if not isinstance(value, dict):
                    raise ValueError()
                if not isinstance(value.get("text"), str) or value.get("model") != model:
                    raise ValueError()
                return Completion(
                    text=value["text"],
                    model=answering_model(value, model),
                    provider=self.name,
                    input_tokens=value.get("inputTokens"),
                    output_tokens=value.get("outputTokens"),
                )
        except (ValueError, OSError, httpx.HTTPError):
            raise ProviderUnavailable(
                "Claude Code could not finish. Check native sign-in and subscription limits."
            ) from None


def configured(environment, timeout):
    url = environment.get("PI_SUBSCRIPTION_URL", "")
    return (
        {
            "claude-code": ClaudeCodeProvider(
                "",
                timeout,
                transport=PrivateTransport(
                    url, environment.get("PI_SUBSCRIPTION_KEY", ""), prefix="/inference/claude"
                ),
                request_cancellation=True,
            )
        }
        if url
        else {}
    )
