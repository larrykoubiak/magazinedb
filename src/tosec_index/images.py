from __future__ import annotations

from PIL import Image


def constrain_image(image: Image.Image, max_dimension: int) -> tuple[float, float]:
    """Resize in place and return factors mapping resized coordinates to the original."""
    original_width, original_height = image.size
    if max_dimension > 0 and max(image.size) > max_dimension:
        image.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
    return original_width / image.width, original_height / image.height
