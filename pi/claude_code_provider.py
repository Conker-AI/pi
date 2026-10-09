"""Text-only adapter to the official native CLI; no borrowed OAuth tokens."""

import httpx

from . import inference_control
from .chatgpt_provider import ChatGPTProvider
from .private_transport import PrivateTransport
from .providers import Completion, ProviderUnavailable, require_images


class ClaudeCodeProvider(ChatGPTProvider):
    name = "claude-code"

    def complete_bounded(self, messages, *, model, timeout):
        require_images(self, messages)
        try:
            with self.cancelable_request() as cancellation, self.client(min(timeout, 180) + 5) as client:
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
                if not isinstance(value.get("text"), str) or value.get("model") != model:
                    raise ValueError()
                return Completion(
                    text=value["text"],
                    model=value.get("actualModel") or model,
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
