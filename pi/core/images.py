"""An image a model reads: validated raster bytes, base64, attached by the owner."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ImageInput:
    media_type: str
    data: str = field(repr=False)
    attachment_id: str = ""
