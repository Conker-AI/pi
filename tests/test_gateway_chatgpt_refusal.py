"""The ChatGPT host control's refusals reach the owner as written (hunt pi-gateway-1 F3)."""

import httpx
import pytest

from gateway import chatgpt
from gateway.provider_control import ProviderControlError


def test_a_refused_operation_says_not_confirmed_not_invalid_response():
    transport = httpx.MockTransport(lambda request: httpx.Response(409, json={}))
    with pytest.raises(ProviderControlError, match="was not confirmed"):
        chatgpt.request("/unused.sock", "GET", transport=transport)
