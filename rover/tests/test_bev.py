"""rover/rover/bev.py: the BEV convention, pixel->metre, and the station mosaic.

P3's area (docs/04 section 3). These tests exist to pin the convention in the bev.py
docstring, because every error it can make is silent: a sign flip puts rocks behind the
rover and a wrong rotation direction smears the mosaic, and neither raises.
"""

from __future__ import annotations

import numpy as np
import pytest

from rover.bev import (
    BevError,
    pixel_to_rover_xy,
    rover_to_bev_px,
    stitch_station,
    warp_to_bev,
)

SIZE = 360  # calib.yaml bev_size_px
RES = 0.005  # calib.yaml bev_res_m_px -> 1.8 m square
C = SIZE / 2  # the rover's pixel
EYE = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))

# A deliberately non-identity homography: image (u,v) -> bev (2u + 10, 2v + 20). Identity
# would pass even if the function ignored H entirely.
SCALED = ((2.0, 0.0, 10.0), (0.0, 2.0, 20.0), (0.0, 0.0, 1.0))


# --- the convention -----------------------------------------------------------------------


def test_centre_pixel_is_the_rover():
    assert pixel_to_rover_xy(C, C, EYE, RES, SIZE) == pytest.approx((0.0, 0.0))


def test_up_is_forward_and_left_is_left():
    """The whole convention in two assertions: -v is +fwd, -u is +lat."""
    fwd, lat = pixel_to_rover_xy(C, C - 80, EYE, RES, SIZE)
    assert fwd == pytest.approx(0.4)  # 80 px * 0.005 m/px, ahead
    assert lat == pytest.approx(0.0)

    fwd, lat = pixel_to_rover_xy(C - 80, C, EYE, RES, SIZE)
    assert fwd == pytest.approx(0.0)
    assert lat == pytest.approx(0.4)  # to the rover's left


def test_down_and_right_are_behind_and_right():
    fwd, lat = pixel_to_rover_xy(C + 40, C + 100, EYE, RES, SIZE)
    assert fwd == pytest.approx(-0.5)  # behind the rover
    assert lat == pytest.approx(-0.2)  # to its right


def test_homography_is_actually_applied():
    """Hand-computed: image (85, 80) -> bev (2*85+10, 2*80+20) = (180, 180) = the rover."""
    assert pixel_to_rover_xy(85, 80, SCALED, RES, SIZE) == pytest.approx((0.0, 0.0))
    # image (85, 30) -> bev (180, 80) -> 100 px above centre -> 0.5 m ahead
    assert pixel_to_rover_xy(85, 30, SCALED, RES, SIZE) == pytest.approx((0.5, 0.0))


def test_rover_to_bev_px_inverts_pixel_to_rover_xy():
    for u, v in ((C, C), (12.0, 300.0), (359.0, 1.0)):
        fwd, lat = pixel_to_rover_xy(u, v, EYE, RES, SIZE)
        assert rover_to_bev_px(fwd, lat, RES, SIZE) == pytest.approx((u, v))


# --- warp ---------------------------------------------------------------------------------


def test_warp_returns_a_bgra_square_with_transparent_border():
    img = np.full((60, 80, 3), 200, dtype=np.uint8)
    out = warp_to_bev(img, EYE, SIZE)
    assert out.shape == (SIZE, SIZE, 4)
    # Under identity the 80x60 frame lands in the top-left corner and nothing else is seen.
    assert out[10, 10, 3] == 255
    assert out[SIZE - 10, SIZE - 10, 3] == 0, "unseen ground must stay transparent"


def test_warp_accepts_grey_and_bgra_input():
    for img in (np.zeros((40, 40), np.uint8), np.zeros((40, 40, 4), np.uint8)):
        assert warp_to_bev(img, EYE, SIZE).shape == (SIZE, SIZE, 4)


def test_warp_rejects_a_singular_homography():
    """A bad calibration run, as distinct from the uncalibrated identity default."""
    singular = ((1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    with pytest.raises(BevError, match="singular"):
        warp_to_bev(np.zeros((10, 10, 3), np.uint8), singular, SIZE)


def test_warp_rejects_a_bad_shape_or_size():
    with pytest.raises(BevError, match="3x3"):
        warp_to_bev(np.zeros((10, 10, 3), np.uint8), ((1.0, 0.0), (0.0, 1.0)), SIZE)
    with pytest.raises(BevError, match="positive"):
        warp_to_bev(np.zeros((10, 10, 3), np.uint8), EYE, 0)


# --- the mosaic ---------------------------------------------------------------------------


def _view_with_mark_ahead(dist_px: int = 100, half: int = 6) -> np.ndarray:
    """A transparent BEV with one bright square `dist_px` directly ahead of the rover."""
    view = np.zeros((SIZE, SIZE, 4), dtype=np.uint8)
    cy = int(C - dist_px)
    cx = int(C)
    view[cy - half : cy + half, cx - half : cx + half] = 255
    return view


def _is_bright(out: np.ndarray, u: int, v: int) -> bool:
    return bool(out[v, u, 3] > 128 and out[v, u, 0] > 128)


def test_stitch_keeps_the_first_view_unrotated():
    out = stitch_station([(0.0, _view_with_mark_ahead())], RES, SIZE)
    assert out.shape == (SIZE, SIZE, 4)
    assert _is_bright(out, int(C), int(C - 100))


def test_stitch_rotates_a_ccw_turn_counter_clockwise():
    """The direction check. A view captured after turning +90 deg CCW saw ground that is to
    the station frame's LEFT, so its mark must land left of centre, not right."""
    views = [(0.0, _view_with_mark_ahead()), (90.0, _view_with_mark_ahead())]
    out = stitch_station(views, RES, SIZE)
    assert _is_bright(out, int(C), int(C - 100)), "view 0's mark stays ahead"
    assert _is_bright(out, int(C - 100), int(C)), "view 1's mark belongs to the LEFT"
    assert not _is_bright(out, int(C + 100), int(C)), "...and must not be on the right"


def test_stitch_uses_absolute_headings_not_the_index():
    """A station that starts at 30 deg records 30, 120, ... and the mosaic must come out the
    same as one that started at 0. This is contract 1.3.0's absolute-heading rule, in the
    one place where a relative value would still look plausible."""
    at_zero = stitch_station(
        [(0.0, _view_with_mark_ahead()), (90.0, _view_with_mark_ahead())], RES, SIZE
    )
    at_thirty = stitch_station(
        [(30.0, _view_with_mark_ahead()), (120.0, _view_with_mark_ahead())], RES, SIZE
    )
    assert np.array_equal(at_zero, at_thirty)


def test_stitch_wraps_the_heading_difference():
    """-170 deg after +170 deg is a 20 deg turn, not a 340 deg one (doc 00 section 4)."""
    a = stitch_station(
        [(170.0, _view_with_mark_ahead()), (-170.0, _view_with_mark_ahead())], RES, SIZE
    )
    b = stitch_station([(0.0, _view_with_mark_ahead()), (20.0, _view_with_mark_ahead())], RES, SIZE)
    assert np.array_equal(a, b)


def test_stitch_max_blends_rather_than_overwriting():
    """Later views fill earlier holes; they do not blank them."""
    ahead = _view_with_mark_ahead(dist_px=100)
    nearer = _view_with_mark_ahead(dist_px=40)
    out = stitch_station([(0.0, ahead), (0.0, nearer)], RES, SIZE)
    assert _is_bright(out, int(C), int(C - 100))
    assert _is_bright(out, int(C), int(C - 40))


def test_stitch_resizes_a_mismatched_view():
    small = np.full((90, 90, 4), 255, dtype=np.uint8)
    assert stitch_station([(0.0, small)], RES, SIZE).shape == (SIZE, SIZE, 4)


def test_stitch_needs_a_view():
    with pytest.raises(BevError, match="at least one view"):
        stitch_station([], RES, SIZE)
