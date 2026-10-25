"""Approximate station BEV from keyframes, for a rover whose camera is not calibrated yet.

An uncalibrated Pi (calib.yaml `calibrated_at: null`, `--allow-uncalibrated`) uploads the
station's keyframes but no bev.png, so the map stays empty. This builds one on the GX10 from a
NOMINAL pinhole camera: a height, a downward pitch and a field of view, instead of the
measured homography. Each keyframe is projected onto the ground and the views are combined in
the station frame, using the same convention as rover/rover/bev.py:

    BEV is size x size, the ROVER IS THE CENTRE PIXEL, +x forward is UP, +y left is LEFT,
    one pixel is res metres, and the frame is the rover at the FIRST keyframe's heading.

UNVERIFIED: every camera number below is a guess from docs/01 (mast ~18-22 cm, tilted 30-35
deg down; Camera Module 3 standard lens, ~66 deg horizontal FOV). The tiles look like the
ground the rover saw, roughly where it saw it, but distances can be off by tens of percent.
rover/calib/calibrate_camera.py replaces all of this with a measured homography; once it has
run, the Pi sends its own bev.png and this module is never called.

Override the guesses with env vars (no restart of the Pi needed, only the mapper):
    MAPPER_CAM_HEIGHT_M   0.20   lens height above the ground
    MAPPER_CAM_PITCH_DEG  32     downward tilt of the optical axis
    MAPPER_CAM_HFOV_DEG   66     horizontal field of view
    MAPPER_CAM_X_M        -0.05  lens position ahead of the rover centre (mast is rear-centre)
    MAPPER_APPROX_RANGE_M 0.9    ignore ground further than this from the rover centre
"""

from __future__ import annotations

import io
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class NominalCamera:
    height_m: float = 0.20
    pitch_deg: float = 32.0
    hfov_deg: float = 66.0
    x_m: float = -0.05
    range_m: float = 0.9

    @classmethod
    def from_env(cls) -> NominalCamera:
        def f(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, default))
            except ValueError:
                return default

        d = cls()
        return cls(
            height_m=f("MAPPER_CAM_HEIGHT_M", d.height_m),
            pitch_deg=f("MAPPER_CAM_PITCH_DEG", d.pitch_deg),
            hfov_deg=f("MAPPER_CAM_HFOV_DEG", d.hfov_deg),
            x_m=f("MAPPER_CAM_X_M", d.x_m),
            range_m=f("MAPPER_APPROX_RANGE_M", d.range_m),
        )


def _wrap(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def project_view(
    img: np.ndarray,
    phi_deg: float,
    cam: NominalCamera,
    res_m_px: float,
    size_px: int,
) -> tuple[np.ndarray, np.ndarray]:
    """One RGB keyframe onto the station-frame ground grid.

    `phi_deg` is this view's heading minus the station's first heading (CCW positive).
    Returns (rgb, depth): rgb is size x size x 3, depth is the optical-axis distance of each
    ground pixel (inf where the camera did not see it).
    """
    h_img, w_img = img.shape[:2]
    c = size_px / 2.0
    idx = np.arange(size_px, dtype=np.float64)
    bu, bv = np.meshgrid(idx + 0.5, idx + 0.5)  # pixel centres; bu = column, bv = row
    fwd0 = (c - bv) * res_m_px
    lat0 = (c - bu) * res_m_px

    # Station frame -> this view's rover frame: undo the view's CCW rotation by phi.
    p = math.radians(phi_deg)
    cp, sp = math.cos(p), math.sin(p)
    x = cp * fwd0 + sp * lat0
    y = -sp * fwd0 + cp * lat0

    # Ground point -> camera (OpenCV axes: z along the optical axis, x right, y down).
    th = math.radians(cam.pitch_deg)
    ct, st = math.cos(th), math.sin(th)
    dx = x - cam.x_m
    xc = -y
    yc = -dx * st + cam.height_m * ct
    zc = dx * ct + cam.height_m * st

    f = (w_img / 2.0) / math.tan(math.radians(cam.hfov_deg) / 2.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        u = f * xc / zc + w_img / 2.0
        v = f * yc / zc + h_img / 2.0
    seen = (
        (zc > 0.02)
        & (u >= 0)
        & (u < w_img)
        & (v >= 0)
        & (v < h_img)
        & (np.hypot(fwd0, lat0) <= cam.range_m)
    )
    ui = np.clip(u, 0, w_img - 1).astype(np.int64)
    vi = np.clip(v, 0, h_img - 1).astype(np.int64)
    rgb = np.zeros((size_px, size_px, 3), dtype=np.uint8)
    rgb[seen] = img[vi[seen], ui[seen]]
    depth = np.where(seen, zc, np.inf)
    return rgb, depth


def approx_station_bev(
    keyframes: Sequence[tuple[float, bytes]],
    res_m_px: float,
    size_px: int,
    cam: NominalCamera | None = None,
) -> bytes:
    """[(absolute_heading_deg, jpeg_bytes), ...] in capture order -> RGBA PNG bytes.

    Where views overlap, the one that saw the ground closest (smallest depth, so the most
    pixels per metre) wins. Ground no view saw stays transparent, as in a real bev.png.
    Raises ValueError when no keyframe is a readable image.
    """
    cam = cam or NominalCamera.from_env()
    best_rgb = np.zeros((size_px, size_px, 3), dtype=np.uint8)
    best_depth = np.full((size_px, size_px), np.inf)
    heading0: float | None = None
    for heading, jpg in keyframes:
        try:
            img = np.asarray(Image.open(io.BytesIO(jpg)).convert("RGB"))
        except OSError:
            continue
        if heading0 is None:
            heading0 = float(heading)
        rgb, depth = project_view(img, _wrap(float(heading) - heading0), cam, res_m_px, size_px)
        closer = depth < best_depth
        best_rgb[closer] = rgb[closer]
        best_depth[closer] = depth[closer]
    if heading0 is None:
        raise ValueError("no readable keyframe to build an approximate BEV from")
    alpha = np.where(np.isfinite(best_depth), 255, 0).astype(np.uint8)
    out = Image.fromarray(np.dstack([best_rgb, alpha]), "RGBA")
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
