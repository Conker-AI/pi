"""Validate explicitly attached raster bytes; never fetch image URLs or execute SVG."""

import base64
import io

from PIL import Image

from .providers import ImageInput

FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


def prepare(raw, media_type, identity):
    try:
        if media_type not in FORMATS or len(raw) > 5 * 1024 * 1024:
            raise ValueError()
        with Image.open(io.BytesIO(raw)) as picture:
            width, height = picture.size
            if (
                picture.format != FORMATS[media_type]
                or getattr(picture, "n_frames", 1) != 1
                or not 1 <= width <= 8192
                or not 1 <= height <= 8192
                or width * height > 4_194_304
            ):
                raise ValueError()
            picture.load()
        return ImageInput(media_type, base64.b64encode(raw).decode("ascii"), identity)
    except Exception:
        raise ValueError(
            "Use a valid still PNG, JPEG or WebP, at most 5 MiB and 4 megapixels."
        ) from None
