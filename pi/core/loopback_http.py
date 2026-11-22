"""Avoid unrelated CA loading for explicitly addressed loopback HTTP only."""

import ssl

import httpx


def verification(url: str | httpx.URL) -> ssl.SSLContext | bool:
    """Select verification without changing destination or transport authority.

    HTTP has no TLS handshake. A fresh, empty trust store saves loading CA
    roots for numeric loopback HTTP, while still refusing accidental HTTPS.
    Every other destination retains HTTPX's default verified TLS behavior.
    Callers must retain their own proxy, redirect and credential restrictions.
    """
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError):
        return True
    if parsed.scheme == "http" and parsed.host in {"127.0.0.1", "::1"} and not parsed.userinfo:
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return True
