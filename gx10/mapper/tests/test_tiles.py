from __future__ import annotations

import base64
import io

import numpy as np
import pytest
from PIL import Image, UnidentifiedImageError

from ..tiles import THUMB_MAX_CHARS, TILE_MAX_CHARS, bev_tile, png_b64, thumb_b64
from .helpers import bev_png, noise_jpg


def _img(b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(b64)))


def test_thumb_is_small_jpeg_keeping_aspect():
    out = thumb_b64(noise_jpg(1280, 720))
    img = _img(out)
    assert img.format == "JPEG"
    assert img.size == (320, 180)
    assert len(out) <= THUMB_MAX_CHARS


def test_thumb_never_exceeds_limit_even_when_asked_for_a_big_one():
    out = thumb_b64(noise_jpg(1600, 1600), max_px=1600, quality=95)
    assert len(out) <= THUMB_MAX_CHARS


def test_thumb_does_not_upscale_small_images():
    assert _img(thumb_b64(noise_jpg(200, 100))).size == (200, 100)


def test_thumb_rejects_non_images():
    with pytest.raises(UnidentifiedImageError):
        thumb_b64(b"not a jpeg")


def test_png_b64_caps_size_and_keeps_alpha():
    arr = np.zeros((1000, 600, 4), dtype=np.uint8)
    img = _img(png_b64(arr))
    assert img.format == "PNG" and img.size == (307, 512) and img.mode == "RGBA"


def test_bev_tile_small_image_is_unchanged():
    tile = bev_tile(bev_png(360, 360), 0.005)
    assert (tile.w_px, tile.h_px, tile.res_m_px) == (360, 360, 0.005)
    img = _img(tile.png_b64)
    assert img.size == (360, 360) and img.mode == "RGBA"
    assert img.getpixel((10, 350))[3] == 0  # unseen area stays transparent


def test_bev_tile_large_image_is_shrunk_keeping_metric_size():
    tile = bev_tile(bev_png(1024, 1024), 0.005)
    assert (tile.w_px, tile.h_px) == (512, 512)
    assert tile.res_m_px * tile.w_px == pytest.approx(1024 * 0.005)


def test_bev_tile_noisy_image_fits_reducer_limit():
    tile = bev_tile(bev_png(512, 512, noise=True), 0.005)
    assert len(tile.png_b64) <= TILE_MAX_CHARS
    assert tile.res_m_px * tile.w_px == pytest.approx(512 * 0.005, rel=1e-2)


def test_bev_tile_rgb_stays_rgb():
    assert _img(bev_tile(bev_png(100, 100, alpha=False), 0.005).png_b64).mode == "RGB"
