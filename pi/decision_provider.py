"""Replaceable typed routing service adapter; never an answer/text provider."""
from __future__ import annotations

import json
import math
from urllib.parse import urlsplit

import httpx

from .providers import Completion, ProviderUnavailable


class DecisionProvider:
    name = "decisions"
    supports_images = False

    def __init__(self, url, key, *, transport=None):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
            raise ValueError("Decision service requires HTTPS or local loopback HTTP.")
        if len(key) < 32:
            raise ValueError("Decision service key requires at least 32 characters.")
        self.url, self.key, self.transport = url.rstrip("/"), key, transport

    def complete_bounded(self, messages, *, model, timeout):
        # This adapter only accepts Pi's typed routing envelope. It must fail
        # if selected for answering, summaries or arbitrary prompt evaluation.
        if model != "model-routing" or len(messages) != 2 or messages[-1].role != "user":
            raise ProviderUnavailable("Decision adapter supports typed model routing only.")
        try:
            envelope = json.loads(messages[-1].content)
            allowed = envelope["allowedModelIds"]
            descriptions = envelope["modelDescriptions"]
            if (not isinstance(allowed, list) or not 2 <= len(allowed) <= 8
                    or len(set(allowed)) != len(allowed) or set(descriptions) != set(allowed)):
                raise ValueError()
            state = json.dumps(envelope["task"], ensure_ascii=False)
            if len(state) > 12000:
                raise ValueError()
            with httpx.Client(transport=self.transport, trust_env=False,
                              timeout=min(max(timeout, 0.1), 30), follow_redirects=False) as client:
                response = client.post(self.url + "/v1/choose", headers={"X-Decision-Key": self.key},
                    json={"state": state,
                          "instructions": "Choose the model best suited to this task using the capability descriptions.",
                          "choices": descriptions})
                response.raise_for_status()
                if len(response.content) > 16384:
                    raise ValueError()
                data = response.json()
            if (data["choice"] not in allowed or type(data["confidence"]) not in {int, float}
                    or not math.isfinite(data["confidence"]) or not 0 <= data["confidence"] <= 1
                    or not isinstance(data["model"], str) or not 1 <= len(data["model"]) <= 200):
                raise ValueError()
            return Completion(text=json.dumps({"modelId": data["choice"]}),
                              model=data["model"], provider=self.name,
                              raw={"decision": {key: data.get(key) for key in
                                   ("choice", "confidence", "elapsed_ms", "provider", "model")}})
        except (ValueError, TypeError, KeyError, httpx.HTTPError):
            raise ProviderUnavailable("Typed decision service unavailable or input unsupported.") from None


def configured(environment):
    url, key = environment.get("PI_DECISION_URL", ""), environment.get("PI_DECISION_KEY", "")
    if not url and not key:
        return {}
    return {"decisions": DecisionProvider(url, key)}
