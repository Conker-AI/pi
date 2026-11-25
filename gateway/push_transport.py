"""The push client's only way out: one DNS answer, checked, then TLS to the push service's name.

`push.endpoint_allowed` keeps endpoints to the browsers' push services by name, and the client
verifies TLS, follows no redirect and takes no proxy from the environment. A name is not an
address, though: DNS for an allowed name could answer with an address inside the server's network
(a changed record, a resolver the host does not control), and a connection or handshake would
reach it. So each new connection resolves the push host once and refuses the whole answer when
any address in it is not on the public internet: private, loopback, link-local, multicast,
reserved, unspecified or shared, or an IPv6 address that carries such an IPv4 one (mapped, 6to4,
Teredo, NAT64). Then it connects to a checked address itself, with no second lookup, and TLS still
names the real host: SNI and the certificate check use the endpoint's hostname, so the far end
must prove it is that push service.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
from collections.abc import Callable, Iterable
from typing import Any

import httpcore
import httpx

NAT64 = ipaddress.ip_network("64:ff9b::/96")


def public(address: str) -> bool:
    """Whether one resolved address is a public internet address, and so is any IPv4 address it
    carries."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    judged: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        judged += [inner for inner in (ip.ipv4_mapped, ip.sixtofour) if inner]
        judged += list(ip.teredo or ())
        if ip in NAT64:
            judged.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return all(
        each.is_global
        and not (
            each.is_private
            or each.is_loopback
            or each.is_link_local
            or each.is_multicast
            or each.is_reserved
            or each.is_unspecified
        )
        for each in judged
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
