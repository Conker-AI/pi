"""Dedicated MemoryGate correction capability; never uses an admin/read/ingest key."""

from urllib.parse import quote

import httpx


class CorrectionError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__("Memory correction transport failed.")


class Client:
    def __init__(self, url, key, agent_id, *, transport=None):
        if len(key) < 32 or not agent_id or not url:
            raise ValueError("Configure correction URL, key and namespace together.")
        self.agent_id = agent_id
        self.http = httpx.Client(
            base_url=url.rstrip("/"),
            timeout=5,
            follow_redirects=False,
            headers={"X-MemoryGate-Correction-Key": key, "X-Agent-Id": agent_id},
            transport=transport,
        )

    def _request(self, method, path, **kwargs):
        with self.http.stream(method, "/runtime/corrections/" + path, **kwargs) as response:
            if response.status_code != 200:
                raise CorrectionError(response.status_code)
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 100000:
                    raise ValueError("Correction response exceeds limit.")
        import json

        result = json.loads(raw)
        if not isinstance(result, dict) or result.get("agent_id") != self.agent_id:
            raise ValueError("Correction namespace mismatch.")
        return result

    def memory(self, identity):
        result = self._request("GET", "memories/" + quote(identity, safe=""))
        if (
            result.get("id") != identity
            or type(result.get("revision")) is not int
            or result["revision"] < 1
            or not isinstance(result.get("text"), str)
            or len(result["text"]) > 16000
        ):
            raise ValueError("Invalid memory snapshot.")
        return {
            key: result.get(key)
            for key in ("id", "agent_id", "revision", "text", "source_type", "confidence")
        }

    def apply(self, identity, memory_id, revision, text):
        return self._request(
            "PUT",
            quote(identity, safe=""),
            json={"memory_id": memory_id, "expected_revision": revision, "text": text},
        )

    def receipt(self, identity):
        return self._request("GET", quote(identity, safe=""))

    def forget(self, identity, memory_id, revision):
        return self._request(
            "PUT",
            "forget/" + quote(identity, safe=""),
            json={"memory_id": memory_id, "expected_revision": revision},
        )

    def close(self):
        self.http.close()
