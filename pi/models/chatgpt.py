"""ChatGPT subscription inference. Auth lives in Conker's private host worker."""

import json
import uuid
from contextlib import ExitStack, contextmanager

import httpx

from pi.conversation import live_stream
from pi.models.providers import Completion, ProviderUnavailable, require_images
from pi.turns import inference_control

ERRORS = {
    "not_connected": "Sign in with ChatGPT in Settings > Providers.",
    "sign_in_required": "ChatGPT sign-in expired. Reconnect in Settings > Providers.",
    "usage_limit": "ChatGPT usage limit reached.",
}


class ChatGPTProvider:
    name = "chatgpt"
    supports_images = False
    allow_paid = False

    def __init__(self, socket, timeout=180, *, transport=None, request_cancellation=False):
        self.socket, self.timeout = socket, timeout
        self.transport = transport
        self.request_cancellation = request_cancellation

    def client(self, timeout):
        return httpx.Client(
            transport=self.transport or httpx.HTTPTransport(uds=self.socket),
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )

    def health(self):
        try:
            with self.client(5) as client:
                value = client.get("http://conker-chatgpt/status").json()
            if (
                set(value) != {"connected", "available"}
                or type(value["connected"]) is not bool
                or type(value["available"]) is not bool
            ):
                raise ValueError()
            # Auth is not an entitlement or successful-inference receipt.
            return {
                "status": "unverified"
                if value["connected"]
                else "not_configured"
                if value["available"]
                else "unavailable"
            }
        except (httpx.HTTPError, OSError, ValueError):
            return {"status": "unavailable"}

    def plan_limits(self):
        """The plan's usage in percent per window, from the runtime; reads only."""
        with self.client(60) as client:
            result = client.get("http://conker-chatgpt/limits")
            result.raise_for_status()
            if len(result.content) > 64_000:
                raise ValueError("Plan usage response is too large.")
            return result.json()

    def complete(self, messages, *, model):
        return self.complete_bounded(messages, model=model, timeout=self.timeout)

    @contextmanager
    def cancelable_request(self):
        token = inference_control.current()
        if token is None or not self.request_cancellation:
            yield {}
            return
        request_id = token.request_id + "_" + uuid.uuid4().hex

        def cancel():
            # The worker owns the native process/upstream connection. Never close
            # its shared transport or another request's inference.
            with self.client(2) as client:
                client.post("http://conker-chatgpt/cancel", json={"request_id": request_id})

        with token.register(cancel):
            yield {"request_id": request_id}

    def complete_bounded(self, messages, *, model, timeout):
        require_images(self, messages)
        payload = {
            "model": model,
            "messages": [{"role": row.role, "content": row.content} for row in messages],
            "timeout": min(max(timeout, 1), self.timeout, 180),
        }
        text, done = [], None
        size = 0
        live_stream.begin_attempt()
        try:
            with ExitStack() as stack:
                payload.update(stack.enter_context(self.cancelable_request()))
                client = stack.enter_context(self.client(payload["timeout"] + 5))
                response = stack.enter_context(
                    client.stream("POST", "http://conker-chatgpt/responses", json=payload)
                )
                token = inference_control.current()
                if token is not None:
                    stack.enter_context(
                        token.register(lambda: inference_control.close_response(response))
                    )
                inference_control.check()
                response.raise_for_status()
                if response.headers.get("content-type") != "application/x-ndjson":
                    raise ValueError()
                for line in response.iter_lines():
                    inference_control.check()
                    if len(line) > 2_000_000:
                        raise ValueError()
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError()
                    live_stream.raise_if_stopped()
                    if event.get("type") == "error":
                        raise ProviderUnavailable(
                            ERRORS.get(event.get("code"), "ChatGPT provider is unavailable.")
                        )
                    if (
                        event.get("type") == "text"
                        and set(event) == {"type", "delta"}
                        and isinstance(event["delta"], str)
                        and done is None
                    ):
                        size += len(event["delta"])
                        if size > 200_000:
                            raise ValueError()
                        text.append(event["delta"])
                        live_stream.delta(event["delta"])
                    elif (
                        event.get("type") == "done"
                        and set(event)
                        == {"type", "model", "inputTokens", "outputTokens", "cachedTokens"}
                        and done is None
                    ):
                        if event["model"] != model or any(
                            value is not None
                            and (type(value) is not int or not 0 <= value <= 1_000_000_000)
                            for value in [
                                event[key]
                                for key in ("inputTokens", "outputTokens", "cachedTokens")
                            ]
                        ):
                            raise ValueError()
                        done = event
                    else:
                        raise ValueError()
            if done is None or not text:
                raise ValueError()
            return Completion(
                text="".join(text),
                model=model,
                provider=self.name,
                input_tokens=done["inputTokens"],
                output_tokens=done["outputTokens"],
                cached_tokens=done["cachedTokens"],
            )
        except (httpx.HTTPError, OSError, ValueError):
            raise ProviderUnavailable("ChatGPT response was unavailable or incomplete.") from None


def configured(environment, timeout):
    url = environment.get("PI_SUBSCRIPTION_URL", "")
    if url:
        from pi.models.private_transport import PrivateTransport

        return {
            "chatgpt": ChatGPTProvider(
                "",
                timeout,
                transport=PrivateTransport(
                    url, environment.get("PI_SUBSCRIPTION_KEY", ""), prefix="/inference"
                ),
                request_cancellation=True,
            )
        }
    socket = environment.get("PI_CHATGPT_SOCKET", "")
    return {"chatgpt": ChatGPTProvider(socket, timeout)} if socket.startswith("/") else {}
