"""rover/rover/proposals.py: find the rocks, reject what is not one.

P3's area (docs/04 section 3). The fixture is camera.py's SyntheticSource, which already
draws a seeded sand gradient with three dark blobs at known centres -- the same frame
`--camera sim` serves, so these tests and a laptop station run agree by construction.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from rover.camera import SyntheticSource
from rover.proposals import find_rocks

#: SyntheticSource's blobs, (cx, cy, radius). Kept here so a change to the fixture that
#: breaks these tests says which one moved rather than just "found 2, wanted 3".
BLOBS = ((160, 330, 26), (420, 300, 18), (300, 400, 34))


@pytest.fixture
def frame() -> np.ndarray:
    ok, img = SyntheticSource(seed=0).read()
    assert ok
    return img


def test_finds_every_drawn_blob(frame):
    assert len(find_rocks(frame)) == len(BLOBS)


def test_centres_land_inside_the_drawn_circles(frame):
    """Not just "three things": the right three things, in the right places."""
    found = [tuple(p["center_px"]) for p in find_rocks(frame)]
    for cx, cy, r in BLOBS:
        assert any(
            (u - cx) ** 2 + (v - cy) ** 2 <= r * r for u, v in found
        ), f"no proposal inside the blob at ({cx}, {cy})"


def _nearest_blob(p: dict) -> tuple[int, int, int]:
    """The drawn blob a proposal belongs to, so a failure names which one went wrong."""
    u, v = p["center_px"]
    return min(BLOBS, key=lambda b: (u - b[0]) ** 2 + (v - b[1]) ** 2)


def test_bbox_tracks_the_blob_size(frame):
    """A 34 px radius blob should bound to roughly 68 px, not to the whole frame."""
    for prop in find_rocks(frame):
        r = _nearest_blob(prop)[2]
        _, _, w, h = prop["bbox_px"]
        assert 2 * r - 8 <= w <= 2 * r + 8, f"width {w} for a radius-{r} blob"
        assert 2 * r - 8 <= h <= 2 * r + 8, f"height {h} for a radius-{r} blob"


def test_sorted_by_contrast_descending(frame):
    contrasts = [p["contrast"] for p in find_rocks(frame)]
    assert contrasts == sorted(contrasts, reverse=True)


def test_contrast_and_solidity_are_in_range(frame):
    for p in find_rocks(frame):
        assert 0.0 <= p["contrast"] <= 1.0
        assert 0.0 < p["solidity"] <= 1.0
        assert p["contrast"] > 0.1, "a blob 65% darker than the sand is not low-contrast"


def test_flat_ground_proposes_nothing():
    assert find_rocks(np.full((240, 320, 3), 140, dtype=np.uint8)) == []


def test_area_bounds_are_enforced(frame):
    """The bounds must actually bound, in both directions."""
    assert find_rocks(frame, min_area_px=20_000) == []
    assert find_rocks(frame, max_area_px=50) == []
    # The radius-18 blob is ~1017 px^2; the radius-26 one ~2124. A window between them
    # keeps the smallest only.
    only_small = find_rocks(frame, min_area_px=500, max_area_px=1500)
    assert len(only_small) == 1
    assert only_small[0]["center_px"] == pytest.approx([420.5, 300.5], abs=2.0)


def test_a_bright_blob_scores_near_zero():
    """Contrast is signed: a glint is not a rock, and must not outrank one."""
    img = np.full((240, 320, 3), 120, dtype=np.uint8)
    cv2.circle(img, (100, 120), 20, (240, 240, 240), -1)  # bright
    cv2.circle(img, (220, 120), 20, (40, 40, 40), -1)  # dark
    found = find_rocks(img)
    assert len(found) == 2
    assert found[0]["center_px"][0] == pytest.approx(220, abs=3), "the dark blob ranks first"
    assert found[0]["contrast"] > 0.2
    assert found[1]["contrast"] == pytest.approx(0.0, abs=0.01)


@pytest.mark.parametrize(
    "p0,p1,thickness",
    [((40, 60), (280, 200), 3), ((20, 100), (300, 110), 2)],
    ids=["shadow-edge", "sand-ripple"],
)
def test_a_thin_streak_is_rejected_as_not_a_rock(p0, p1, thickness):
    """Elongation is its own filter, not a side effect of solidity.

    A thin diagonal line's convex hull is also a thin diagonal sliver, so a streak scores
    solidity ~0.83 and passes that test -- it was this case that found the gap. The
    min-area rect's aspect ratio is what catches it: ~0.02 for these two, 1.00 for a rock.
    """
    img = np.full((240, 320, 3), 150, dtype=np.uint8)
    cv2.line(img, p0, p1, (40, 40, 40), thickness)
    assert find_rocks(img) == []


def test_a_round_rock_passes_the_aspect_filter_that_rejects_streaks():
    """The other side of the previous test: the filter must not reject real rocks."""
    img = np.full((240, 320, 3), 150, dtype=np.uint8)
    cv2.circle(img, (160, 130), 30, (40, 40, 40), -1)
    found = find_rocks(img)
    assert len(found) == 1
    assert found[0]["aspect"] > 0.9


def test_accepts_grey_and_bgra_frames(frame):
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    bgra = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
    assert len(find_rocks(grey)) == len(BLOBS)
    assert len(find_rocks(bgra)) == len(BLOBS)


def test_is_deterministic(frame):
    """Same frame, same proposals -- a station is replayable or it is not debuggable."""
    assert find_rocks(frame) == find_rocks(frame)
