"""Stable references into immutable extracted text; offsets count Unicode characters."""

import hashlib

SIZE = 1200


def split(identity, text):
    result = []
    for start in range(0, len(text), SIZE):
        end = min(start + SIZE, len(text))
        excerpt = text[start:end]
        result.append(
            {
                "id": f"{identity}:p{len(result)}",
                "start": start,
                "end": end,
                "text": excerpt,
                "sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
            }
        )
    return result
