"""The push client's only way out: one DNS answer, checked, then TLS to the push service's name.

`push.endpoint_allowed` keeps endpoints to the browsers' push services by name, and the client
verifies TLS, follows no redirect and takes no proxy from the environment. A name is not an
address, though: DNS for an allowed name could answer with an address inside the server's network
(a changed record, a resolver the host does not control), and a connection or handshake would
reach it. So each new connection resolves the push host once and refuses the whole answer when
any address in it is not on the public internet: private, loopback, link-local, multicast,
reserved, unspecified, shared or site-local, and an IPv4-mapped IPv6 address is judged by its IPv4 address
too. (6to4, Teredo and NAT64 addresses fall in ranges Python already counts as private or
reserved, so they are refused whatever they carry.) Then it connects to a checked address itself,
with no second lookup, and TLS still names the real host: SNI and the certificate check use the
endpoint's hostname, so the far end must prove it is that push service.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
from collections.abc import Callable, Iterable
from typing import Any

import httpcore
import httpx


def public(address: str) -> bool:
    """Whether one resolved address is a public internet address (a mapped IPv4 one: both)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    mapped = ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) else None
    return all(
        each.is_global
        and not (
            each.is_private
            or each.is_loopback
            or each.is_link_local
            or each.is_multicast
            or each.is_reserved
            or each.is_unspecified
            or (isinstance(each, ipaddress.IPv6Address) and each.is_site_local)
        )
        for each in (ip, mapped)
        if each is not None
    )


class PinnedBackend(httpcore.NetworkBackend):
    """Opens TCP only to an address from one checked DNS answer for the URL's host.

    `resolve` is getaddrinfo's shape; `connect` is httpcore's `connect_tcp`. Both default to the
    system's, looked up at each call."""

    def __init__(self, resolve: Callable[..., list] | None = None, connect=None):
        self.resolve, self.connect = resolve, connect or httpcore.SyncBackend().connect_tcp

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.NetworkStream:
        try:
            answer = (self.resolve or socket.getaddrinfo)(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):  # no answer, or a label DNS cannot carry
            raise httpcore.ConnectError("The push service's name did not resolve.") from None
        addresses = list(dict.fromkeys(str(entry[4][0]) for entry in answer))
        if not addresses or not all(public(address) for address in addresses):
            raise httpcore.ConnectError("The push service's name resolved to a private address.")
        failure: Exception | None = None
        for address in addresses:  # in the resolver's order, as the operating system would
            try:
                return self.connect(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                failure = error
        raise failure or httpcore.ConnectError("The push service could not be reached.")


class PinnedTransport(httpx.HTTPTransport):
    """httpx's own transport, verifying TLS, with every connection made by `PinnedBackend`."""

    def __init__(
        self,
        *,
        verify: ssl.SSLContext | None = None,
        resolve: Callable[..., list] | None = None,
        connect=None,
    ):
        context = verify or httpx.create_ssl_context(trust_env=False)
        super().__init__(verify=context, trust_env=False)
        # The pool HTTPTransport made, with httpx's default limits, but this backend under it.
        self._pool = httpcore.ConnectionPool(
            ssl_context=context,
            max_connections=100,
            max_keepalive_connections=20,
            keepalive_expiry=5.0,
            network_backend=PinnedBackend(resolve, connect),
        )
