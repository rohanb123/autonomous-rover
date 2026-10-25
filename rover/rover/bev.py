"""Bird's-eye-view warp, pixel->metre conversion and the station mosaic.

P3's since 2026-10-24 (docs/04 section 3, commit c6c1aec); written by P2 so station.py has
something to call. Signatures are the ones fixed in docs/roles/P2-rover-runtime.md section 4.

THE BEV CONVENTION. Nothing else in the repo pins this down, and getting it wrong misplaces
every rock and every map tile while nothing raises, so it is stated once here and every
function below obeys it:

    A BEV square is `size_px` x `size_px`. The ROVER IS THE CENTRE PIXEL (size/2, size/2).
    +x_rover (forward) is UP   -- decreasing v.
    +y_rover (left)    is LEFT -- decreasing u.
    One pixel is `res_m_px` metres.

    rover (fwd, lat)  ->  pixel (u, v) = (size/2 - lat/res, size/2 - fwd/res)

Centre and not bottom-centre because a station is a 360 degree spin: at the calib defaults
(`bev_res_m_px: 0.005`, `bev_size_px: 360`) that is a 1.8 m square reaching 0.9 m in every
direction. One front-facing view fills the upper half and leaves the rest transparent.

`stitch_station`'s output is therefore the rover frame at the station's START heading, which
is what `Bev.frame`'s only legal value, "rover@station", means (doc 00 section 6.4).

UNVERIFIED: the mapper turns the mosaic into a tile with
`upsert_local_tile(..., x, y, hdg, res_m_px, ...)` using the station pose (gx10/mapper/app.py).
That the dashboard draws it rover-at-centre is assumed, not confirmed -- noted in
docs/requests/2026-10-24-P2-to-P3-P4-local-map-proposal.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import cv2
import numpy as np

from marsrover_shared.frames import wrap_deg

__all__ = ["BevError", "warp_to_bev", "pixel_to_rover_xy", "stitch_station", "rover_to_bev_px"]


class BevError(ValueError):
    """A homography or a view list that cannot produce a BEV."""


def _as_h(H: Any) -> np.ndarray:
    """A 3x3 float homography from whatever calib.yaml or a test handed us."""
    h = np.asarray(H, dtype=np.float64)
    if h.shape != (3, 3):
        raise BevError(f"homography must be 3x3, got {h.shape}")
    if not np.all(np.isfinite(h)):
        raise BevError("homography contains inf/nan")
    if abs(float(np.linalg.det(h))) < 1e-12:
        # A singular H collapses the ground plane to a line. calib.yaml's identity default is
        # geometrically wrong but NOT singular, so this catches a bad calibration run, not
        # the uncalibrated default -- station.py checks `calib.calibrated` for that.
        raise BevError("homography is singular; re-run rover/calib/calibrate_camera.py")
    return h


def _as_bgra(img: np.ndarray) -> np.ndarray:
    """BGRA with alpha 255, so the warp's transparent border means 'never saw this ground'."""
    if img.ndim == 2:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGRA)
    if img.shape[2] == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    if img.shape[2] == 4:
        return img
    raise BevError(f"expected a 1, 3 or 4 channel image, got shape {img.shape}")


def warp_to_bev(img: np.ndarray, H: Any, size_px: int) -> np.ndarray:
    """Warp a camera frame onto the ground plane. Returns BGRA, `size_px` square.

    Alpha is 0 where no source pixel landed. That matters twice: `stitch_station` uses it to
    fill holes from other views, and the mapper's `bev_tile` keeps alpha so the dashboard
    does not draw black squares over ground the rover never saw.
    """
    if size_px <= 0:
        raise BevError(f"size_px must be positive, got {size_px}")
    return cv2.warpPerspective(
        _as_bgra(img),
        _as_h(H),
        (int(size_px), int(size_px)),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0, 0),
    )


def rover_to_bev_px(
    fwd_m: float, lat_m: float, res_m_px: float, size_px: int
) -> tuple[float, float]:
    """(fwd, lat) metres -> (u, v) pixels. The convention in the module docstring, forwards."""
    c = size_px / 2.0
    return (c - lat_m / res_m_px, c - fwd_m / res_m_px)


def pixel_to_rover_xy(
    u: float, v: float, H: Any, res_m_px: float, size_px: int
) -> tuple[float, float]:
    """An image pixel -> (forward, left) metres in the ROVER frame at capture time.

    The caller converts to the mission frame with `marsrover_shared.frames.rover_to_mission`
    and the pose held at capture -- do not re-derive that rotation here.
    """
    if res_m_px <= 0:
        raise BevError(f"res_m_px must be positive, got {res_m_px}")
    pt = np.array([[[float(u), float(v)]]], dtype=np.float64)
    bu, bv = cv2.perspectiveTransform(pt, _as_h(H))[0][0]
    c = size_px / 2.0
    return ((c - float(bv)) * res_m_px, (c - float(bu)) * res_m_px)


def stitch_station(
    views: Sequence[tuple[float, np.ndarray]], res_m_px: float, size_px: int
) -> np.ndarray:
    """Rotate each view into the station frame and max-blend. Returns BGRA, `size_px` square.

    `views` is [(absolute_heading_deg, bev_bgra), ...] in capture order. The first view's
    heading defines the output frame ("rover@station"), so every other view is rotated by
    `wrap_deg(heading_i - heading_0)`.

    A CCW rover rotation is a CCW rotation about the centre pixel under this convention
    (+x up, +y left), which is exactly `getRotationMatrix2D`'s positive-angle direction --
    so the angle goes in unmodified. Checked by test_bev.py rather than by eye.

    Max-blend because alpha-0 is all-zero: the max of "nothing" and "ground" is the ground,
    so later views fill earlier holes without washing them out. In the overlap it keeps the
    brighter pixel per channel, which can tint a seam slightly; the role doc section 5 asks
    for max-blend and a tinted seam is cheaper than a visible exposure step.
    """
    if not views:
        raise BevError("stitch_station needs at least one view")
    if res_m_px <= 0:
        raise BevError(f"res_m_px must be positive, got {res_m_px}")
    size = int(size_px)
    centre = (size / 2.0, size / 2.0)
    heading0 = float(views[0][0])
    out = np.zeros((size, size, 4), dtype=np.uint8)
    for heading, bev in views:
        tile = _as_bgra(np.asarray(bev))
        if tile.shape[:2] != (size, size):
            tile = cv2.resize(tile, (size, size), interpolation=cv2.INTER_AREA)
        phi = wrap_deg(float(heading) - heading0)
        if abs(phi) > 1e-9:
            M = cv2.getRotationMatrix2D(centre, phi, 1.0)
            tile = cv2.warpAffine(
                tile,
                M,
                (size, size),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=(0, 0, 0, 0),
            )
        np.maximum(out, tile, out=out)
    return out
