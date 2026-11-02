from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps


GEMINI_IMAGE_MAX_EDGE = 1280


def gemini_image_bytes(path: Path, *, max_edge: int = GEMINI_IMAGE_MAX_EDGE) -> bytes:
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image)
        if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            background.alpha_composite(rgba)
            image = background.convert("RGB")
        else:
            image = image.convert("RGB")
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, "JPEG", quality=88, optimize=True)
        return output.getvalue()


def gemini_inline_image_data(path: Path) -> dict[str, str]:
    data = base64.b64encode(gemini_image_bytes(path)).decode("ascii")
    return {"mime_type": "image/jpeg", "data": data}


def gemini_image_data_url(path: Path) -> str:
    data = base64.b64encode(gemini_image_bytes(path)).decode("ascii")
    return f"data:image/jpeg;base64,{data}"
