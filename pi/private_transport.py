"""Authenticated fixed loopback transport for hosts without Unix sockets."""

import httpx


class PrivateTransport(httpx.BaseTransport):
    def __init__(self, url, key, *, prefix="", transport=None):
        parsed = httpx.URL(url)
        if (
            parsed.scheme != "http"
            or parsed.host != "127.0.0.1"
            or parsed.path != "/"
            or parsed.query
            or parsed.fragment
            or parsed.userinfo
            or not parsed.port
            or len(key) < 32
            or any(c in key for c in "\r\n")
        ):
            raise ValueError(
                "Private provider transport requires a fixed loopback origin and scoped key."
            )
        self.url, self.key, self.prefix = parsed, key, prefix
        self.transport = transport or httpx.HTTPTransport()

    def handle_request(self, request):
        if request.url.host not in {"conker-host", "conker-chatgpt"}:
            raise ValueError("Unsupported private provider destination.")
        query = b""
        if request.url.query:
            from .native_codex_query import canonical_query

            if (
                request.method != "GET"
                or request.url.scheme != "http"
                or request.url.host != "conker-host"
                or request.url.userinfo
                or request.url.fragment
                or self.prefix
                or request.url.raw_path.split(b"?", 1)[0] != b"/codex/sessions"
            ):
                raise ValueError("Unsupported private provider destination.")
            query = canonical_query(request.url.query)
        headers = dict(request.headers)
        headers.pop("host", None)
        headers["authorization"] = "Bearer " + self.key
        target = self.url.copy_with(path=self.prefix + request.url.path, query=query or None)
        headers["host"] = target.netloc.decode("ascii")
        return self.transport.handle_request(
            httpx.Request(
                request.method,
                target,
                headers=headers,
                stream=request.stream,
                extensions=request.extensions,
            )
        )

    def close(self):
        # Callers create per-operation clients around this shared transport. Closing
        # one must not cancel another owner's concurrent status/inference request.
        pass

    def shutdown(self):
        self.transport.close()
