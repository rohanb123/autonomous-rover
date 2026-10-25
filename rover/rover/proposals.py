"""Find rock-like blobs in a camera frame. "Rockster-lite".

P3's since 2026-10-24 (docs/04 section 3, commit c6c1aec); written by P2 so station.py has
something to call. Signature is the one fixed in docs/roles/P2-rover-runtime.md section 4.

Named after JPL's Rockster, the onboard rock finder AEGIS uses for target selection on MSL:
edges, then closed contours, then filter on shape. No learning, no model file, no GPU --
the Pi's job is to propose, and gx10/vision's /score decides what is worth driving to.

Runs on the RAW frame, not the BEV warp. The warp smears texture at the far edge of the
ground plane, and the crop the mapper serves to /score should be real pixels.

Returns plain dicts rather than a pydantic model on purpose: these are not a wire message.
station.py turns the ones that survive dedupe into `Proposal`s, which is where the schema
and the p_ ids belong.
"""

from __future__ import annotations

import cv2
import numpy as np

__all__ = ["find_rocks", "DEFAULTS"]

#: Tuned against camera.py's SyntheticSource at 640x480; UNVERIFIED on arena regolith.
#: `calibrate_camera.py` does not touch these -- if the real arena needs different numbers
#: they belong in calib.yaml, which is a contract change, not a constant.
DEFAULTS = {
    "min_area_px": 80,  # ~10x8 px: smaller than this is grain, not a pebble
    "max_area_px": 8000,  # ~90x90 px: bigger is the horizon or a shadow across the frame
    "min_solidity": 0.70,  # area / convex-hull area. Rejects C-shapes and broken rings
    "min_aspect": 0.25,  # short/long side of the min-area rect. Rejects streaks
    "canny_lo": 50,
    "canny_hi": 150,
    "close_px": 5,  # join edges a shadow broke, or one rock becomes three
    "ring_px": 6,  # how far outside the blob to sample the background for contrast
}


def _grey(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return img
    if img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
    if img.shape[2] == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    raise ValueError(f"expected a 1, 3 or 4 channel image, got shape {img.shape}")


def _contrast(grey: np.ndarray, contour: np.ndarray, ring_px: int) -> float:
    """How much darker the blob is than the ground just around it, as 0..1.

    Signed and not absolute: a rock in this lighting is darker than regolith, so a blob that
    is BRIGHTER than its surroundings (a specular glint, a bright sand patch) scores near 0
    and sorts to the bottom instead of competing with real rocks.
    """
    h, w = grey.shape[:2]
    inner = np.zeros((h, w), dtype=np.uint8)
    cv2.drawContours(inner, [contour], -1, 255, thickness=cv2.FILLED)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * ring_px + 1, 2 * ring_px + 1))
    ring = cv2.subtract(cv2.dilate(inner, k), inner)
    if not inner.any() or not ring.any():
        return 0.0
    blob_mean = float(cv2.mean(grey, mask=inner)[0])
    ring_mean = float(cv2.mean(grey, mask=ring)[0])
    return float(np.clip((ring_mean - blob_mean) / 255.0, 0.0, 1.0))


def find_rocks(
    img: np.ndarray,
    min_area_px: int = DEFAULTS["min_area_px"],
    max_area_px: int = DEFAULTS["max_area_px"],
    *,
    min_solidity: float = DEFAULTS["min_solidity"],
    min_aspect: float = DEFAULTS["min_aspect"],
) -> list[dict]:
    """Rock-like blobs, best first.

    Each is `{bbox_px: [x, y, w, h], contrast, solidity, aspect, center_px: [u, v]}`
    -- `bbox_px` straight into `Proposal.bbox_px`, `center_px` through
    `bev.pixel_to_rover_xy` then `frames.rover_to_mission`. `aspect` is extra to the
    role doc's listed keys; station.py ignores it, but it is what rejected a streak.

    Sorted by contrast descending, so a station that finds more than it can crop keeps the
    ones most likely to be rocks.
    """
    grey = _grey(np.asarray(img))
    # Blur before Canny or every grain of sand is an edge.
    blurred = cv2.GaussianBlur(grey, (5, 5), 0)
    edges = cv2.Canny(blurred, DEFAULTS["canny_lo"], DEFAULTS["canny_hi"])
    # Close before findContours: a shadow falling across a rock breaks its outline, and an
    # open contour has no area, so the rock would simply not exist.
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (DEFAULTS["close_px"], DEFAULTS["close_px"]))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k)
    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    out: list[dict] = []
    for c in contours:
        area = float(cv2.contourArea(c))
        if not (min_area_px <= area <= max_area_px):
            continue
        hull_area = float(cv2.contourArea(cv2.convexHull(c)))
        solidity = area / hull_area if hull_area > 0 else 0.0
        if solidity < min_solidity:
            continue  # concave: a broken outline or a ring artefact, not a rock
        # Elongation needs its own test. Solidity does NOT catch a streak: a thin
        # diagonal line's convex hull is also a thin diagonal sliver, so a shadow edge
        # scores ~0.83 and sails through. The min-area rect separates them cleanly --
        # measured 0.02 for a streak and a sand ripple against 1.00 for a round rock.
        (_, _), (rw, rh), _ = cv2.minAreaRect(c)
        longest = max(rw, rh)
        aspect = (min(rw, rh) / longest) if longest > 0 else 0.0
        if aspect < min_aspect:
            continue  # a shadow edge or a sand ripple: long and thin
        x, y, w, h = (int(v) for v in cv2.boundingRect(c))
        out.append(
            {
                "bbox_px": [x, y, w, h],
                "contrast": _contrast(grey, c, DEFAULTS["ring_px"]),
                "solidity": float(solidity),
                "aspect": float(aspect),
                "center_px": [x + w / 2.0, y + h / 2.0],
            }
        )
    out.sort(key=lambda p: p["contrast"], reverse=True)
    return out
