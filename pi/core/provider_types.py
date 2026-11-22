"""The two provider types every package shares: a model input and the failure to answer.

`pi.models.providers` (the provider seam) re-exports both, so its callers keep importing them
from there.
"""

from __future__ import annotations

from dataclasses import dataclass

from pi.core.images import ImageInput
from pi.core.untrusted_text import strip_invisible


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    images: tuple[ImageInput, ...] = ()

    def __post_init__(self):
        # Every model input is a Message, so hidden text (tags, selectors, bidi) stops here
        # whatever it came from: chat, imports, files, memory, webhooks, projects, tools.
        if isinstance(self.content, str):
            object.__setattr__(self, "content", strip_invisible(self.content))


class ProviderUnavailable(RuntimeError):
    """The provider could not answer. The caller degrades; it never invents."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
