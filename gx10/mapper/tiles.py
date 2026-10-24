"""Thumbnails and tile images for SpacetimeDB rows (docs/roles/P3 §4, docs/02 §5).

Base64 strings must stay under the reducer limits: keyframe thumbB64 <= 150,000 chars,
local tile pngB64 <= 700,000 chars. Both helpers shrink the image until they fit.
`slice_mosaic` (global tiles) lands with /fuse.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass

import numpy as np
from PIL import Image

THUMB_MAX_CHARS = 150_000  # upsert_keyframe rejects longer
TILE_MAX_CHARS = 700_000  # upsert_local_tile rejects longer
_MIN_PX = 16


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _jpeg_b64(img: Image.Image, quality: int) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return _b64(buf.getvalue())


def _png_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return _b64(buf.getvalue())


def _shrink(img: Image.Image, max_px: int) -> Image.Image:
    if max(img.size) <= max_px:
        return img
    out = img.copy()
    out.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    return out


def thumb_b64(jpg: bytes, max_px: int = 320, quality: int = 70) -> str:
    """JPEG bytes -> base64 JPEG thumbnail (longest side <= max_px, <= THUMB_MAX_CHARS).

    Raises PIL.UnidentifiedImageError if the bytes aren't an image.
    """
    img = Image.open(io.BytesIO(jpg))
    img.draft("RGB", (max_px, max_px))  # fast JPEG decode at reduced size
    img = _shrink(img.convert("RGB"), max_px)
    while True:
        for q in (quality, 50, 30):
            out = _jpeg_b64(img, q)
            if len(out) <= THUMB_MAX_CHARS:
                return out
        if max(img.size) <= _MIN_PX:
            return out
        img = _shrink(img, max(img.size) // 2)


def png_b64(img: np.ndarray, max_px: int = 512) -> str:
    """Image array (H×W, H×W×3 or H×W×4, uint8) -> base64 PNG, longest side <= max_px."""
    return _png_b64(_shrink(Image.fromarray(img), max_px))


@dataclass(frozen=True)
class Tile:
    png_b64: str
    w_px: int
    h_px: int
    res_m_px: float  # metres per pixel of *this* image (grows when the image is shrunk)


def bev_tile(png: bytes, res_m_px: float, max_px: int = 512) -> Tile:
    """BEV PNG from the Pi -> local tile row data.

    Keeps transparency (areas the camera didn't see) and the tile's metric size: if the image is
    shrunk, res_m_px grows by the same factor. Raises PIL.UnidentifiedImageError on bad bytes.
    """
    src = Image.open(io.BytesIO(png))
    src = src.convert("RGBA" if "A" in src.getbands() or "transparency" in src.info else "RGB")
    width_m = src.width * res_m_px
    img = _shrink(src, max_px)
    while True:
        out = _png_b64(img)
        if len(out) <= TILE_MAX_CHARS or max(img.size) <= _MIN_PX:
            return Tile(out, img.width, img.height, width_m / img.width)
        img = _shrink(img, max(img.size) * 3 // 4)
