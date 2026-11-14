"""Supplied assistant evidence, immutable beside exact message provenance.

A citation is a provider-supplied reference, never independent verification and
never proof that a retrieved memory was actually cited. Links are inert metadata.
"""

from __future__ import annotations

import json
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, TypeAdapter, field_validator

from pi.core.models import Strict


class Citation(Strict):
    id: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=500)
    href: str | None = Field(default=None, max_length=2048)
    excerpt: str | None = Field(default=None, max_length=8000)

    @field_validator("href")
    @classmethod
    def inert_link(cls, value):
        if value:
            parts = urlsplit(value)
            if (
                parts.scheme not in ("https", "http")
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or any(ord(c) < 33 for c in value)
                or "\\" in value
            ):
                raise ValueError("Citations require complete HTTP(S) links without credentials.")
            _ = parts.port
        return value


def normalize(value):
    citations = TypeAdapter(Annotated[list[Citation], Field(max_length=100)]).validate_python(
        [] if value is None else value, strict=True
    )
    result = [item.model_dump(exclude_none=True) for item in citations]
    serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len({item["id"] for item in result}) != len(result):
        raise ValueError("Citation identities must be distinct.")
    if len(serialized.encode("utf-16-le")) // 2 > 100_000:
        raise ValueError("Citations exceed 100,000 serialized characters.")
    return result


def from_openrouter_annotations(value):
    """Map documented Chat Completion URL annotations; never enable retrieval.

    https://openrouter.ai/docs/guides/features/plugins/web-search#parsing-web-search-results
    IDs identify supplied annotation positions within this exact message.
    """
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("Provider annotations must be a bounded list.")
    result = []
    for index, annotation in enumerate(value):
        if not isinstance(annotation, dict):
            raise ValueError("Invalid provider annotation.")
        if annotation.get("type") != "url_citation":
            continue
        supplied = annotation.get("url_citation")
        if not isinstance(supplied, dict):
            raise ValueError("Invalid URL citation annotation.")
        item = {
            "id": f"url-citation-{index + 1}",
            "label": supplied.get("title"),
            "href": supplied.get("url"),
        }
        if not isinstance(item["href"], str) or not item["href"]:
            raise ValueError("URL citation annotation needs a URL.")
        if "content" in supplied:
            item["excerpt"] = supplied["content"]
        result.append(item)
    return normalize(result)
