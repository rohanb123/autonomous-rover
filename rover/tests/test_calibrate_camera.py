"""rover/calib/calibrate_camera.py: the geometry, the guard rails, and the file rewrite.

The clicking UI is not tested (it needs a window); everything it feeds is. The important
cases are the ones where a wrong answer still looks like a right one: corners clicked out
of order, a target that does not fit the BEV square, and a calib.yaml rewrite that loses
the comments explaining which numbers are measurements.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "shared" / "py")]

from rover.bev import pixel_to_rover_xy  # noqa: E402
from rover.config import parse_calib  # noqa: E402

# Not an importable package (it is a script in rover/calib/), so load it by path.
_spec = importlib.util.spec_from_file_location(
    "calibrate_camera", _ROOT / "rover" / "calib" / "calibrate_camera.py"
)
assert _spec and _spec.loader
cc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc)

RES = 0.005
SIZE = 360
NEAR, WIDTH, DEPTH = 0.25, 0.297, 0.210

# A plausible set of clicks for a rectangle on the ground ahead of the rover: the near edge
# is lower and wider in the image, the far edge higher and narrower. In CORNER_ORDER, so
# near-left is on the image's LEFT (the rover's left is -u under the bev convention).
CLICKS = [(140.0, 430.0), (500.0, 430.0), (400.0, 300.0), (240.0, 300.0)]


# --- the geometry it is solving for -------------------------------------------------------


def test_target_corners_match_the_rover_frame_convention():
    """+x forward, +y left, centred laterally (doc 00 section 4)."""
    nl, nr, fr, fl = cc.target_corners_m(NEAR, WIDTH, DEPTH)
    assert nl == pytest.approx((0.25, 0.1485))  # near, LEFT is +y
    assert nr == pytest.approx((0.25, -0.1485))
    assert fr == pytest.approx((0.46, -0.1485))  # far = near + depth
    assert fl == pytest.approx((0.46, 0.1485))


def test_the_homography_puts_the_corners_where_they_physically_are():
    """The whole point: click a known rectangle, recover its real metres."""
    H = cc.homography_from_clicks(CLICKS, NEAR, WIDTH, DEPTH, RES, SIZE)
    want = cc.target_corners_m(NEAR, WIDTH, DEPTH)
    for (u, v), (fwd, lat) in zip(CLICKS, want, strict=True):
        assert pixel_to_rover_xy(u, v, H, RES, SIZE) == pytest.approx((fwd, lat), abs=1e-6)


def test_residuals_are_tiny_for_a_sane_fit():
    H = cc.homography_from_clicks(CLICKS, NEAR, WIDTH, DEPTH, RES, SIZE)
    worst = max(cc.residuals_m(H, CLICKS, NEAR, WIDTH, DEPTH, RES, SIZE))
    assert worst < 1e-6


def test_a_point_between_the_near_corners_lands_on_the_near_edge():
    """Independent of the four fitted points: the midpoint of the near edge must come back
    at lat 0 and fwd = near."""
    H = cc.homography_from_clicks(CLICKS, NEAR, WIDTH, DEPTH, RES, SIZE)
    mid_u = (CLICKS[0][0] + CLICKS[1][0]) / 2
    got, err = cc.check_point_error_m(H, (mid_u, 430.0), (NEAR, 0.0), RES, SIZE)
    assert got == pytest.approx((NEAR, 0.0), abs=1e-6)
    assert err < 1e-6


# --- the guard rails ----------------------------------------------------------------------


def test_corners_clicked_out_of_order_are_rejected():
    """The failure this exists for. Swapping two corners gives a bow tie: a perfectly
    invertible matrix that maps the ground to nonsense, which nothing downstream can see."""
    swapped = [CLICKS[0], CLICKS[2], CLICKS[1], CLICKS[3]]
    with pytest.raises(cc.CalibrationError, match="convex"):
        cc.homography_from_clicks(swapped, NEAR, WIDTH, DEPTH, RES, SIZE)


def test_collinear_corners_are_rejected():
    flat = [(100.0, 400.0), (200.0, 400.0), (300.0, 400.0), (400.0, 400.0)]
    with pytest.raises(cc.CalibrationError, match="collinear"):
        cc.homography_from_clicks(flat, NEAR, WIDTH, DEPTH, RES, SIZE)


def test_a_target_beyond_the_bev_square_is_rejected():
    """At 0.005 m/px and 360 px the square reaches 0.9 m. A target 2 m out would be warped
    clean off the tile, and the warp itself would not complain."""
    with pytest.raises(cc.CalibrationError, match="does not fit"):
        cc.homography_from_clicks(CLICKS, 2.0, WIDTH, DEPTH, RES, SIZE)


def test_wrong_number_of_corners_is_rejected():
    with pytest.raises(cc.CalibrationError, match="4 corners"):
        cc.homography_from_clicks(CLICKS[:3], NEAR, WIDTH, DEPTH, RES, SIZE)


def test_parse_corners_reads_the_headless_form():
    assert cc.parse_corners("100,400 540,400 420,300 220,300") == [
        (100.0, 400.0),
        (540.0, 400.0),
        (420.0, 300.0),
        (220.0, 300.0),
    ]


@pytest.mark.parametrize(
    "spec,needle",
    [("1,2 3,4", "4 points"), ("1,2 3,4 5,6 seven", "cannot read")],
    ids=["too-few", "not-a-number"],
)
def test_parse_corners_rejects_junk(spec, needle):
    with pytest.raises(cc.CalibrationError, match=needle):
        cc.parse_corners(spec)


# --- the calib.yaml rewrite ---------------------------------------------------------------


def test_rewrite_replaces_the_homography_and_the_stamp(tmp_path):
    src = (_ROOT / "rover" / "calib" / "calib.yaml").read_text(encoding="utf-8")
    H = cc.homography_from_clicks(CLICKS, NEAR, WIDTH, DEPTH, RES, SIZE)
    out = cc.rewrite_calib(src, H, "2026-10-24T12:00:00+00:00")

    calib = parse_calib(__import__("yaml").safe_load(out))
    assert calib.calibrated_at == "2026-10-24T12:00:00+00:00"
    assert calib.calibrated is True
    assert np.allclose(np.array(calib.homography), H)
    # The placeholder identity must be gone, or station.py would upload fiction believing
    # it had been calibrated.
    assert not np.allclose(np.array(calib.homography), np.eye(3))


def test_rewrite_keeps_the_comments():
    """calib.yaml is half comments explaining which numbers are real. A yaml round-trip
    would drop all of them, which is why the rewrite is textual."""
    src = (_ROOT / "rover" / "calib" / "calib.yaml").read_text(encoding="utf-8")
    H = cc.homography_from_clicks(CLICKS, NEAR, WIDTH, DEPTH, RES, SIZE)
    out = cc.rewrite_calib(src, H, "2026-10-24T12:00:00+00:00")
    for marker in ("READ THIS BEFORE TRUSTING A NUMBER", "UNVERIFIED", "L293D pin 1"):
        assert marker in out, f"the rewrite lost {marker!r}"
    # Nothing outside the two edited regions moved.
    assert out.count("UNVERIFIED") >= src.count("UNVERIFIED") - 1


def test_rewrite_is_idempotent():
    """Running the calibration twice must not nest or duplicate the block."""
    src = (_ROOT / "rover" / "calib" / "calib.yaml").read_text(encoding="utf-8")
    H = cc.homography_from_clicks(CLICKS, NEAR, WIDTH, DEPTH, RES, SIZE)
    once = cc.rewrite_calib(src, H, "2026-10-24T12:00:00+00:00")
    twice = cc.rewrite_calib(once, H, "2026-10-25T09:30:00+00:00")
    assert twice.count("homography:") == 1
    assert twice.count("calibrated_at:") == once.count("calibrated_at:")
    assert '"2026-10-25T09:30:00+00:00"' in twice


def test_rewrite_refuses_a_file_without_the_block():
    with pytest.raises(cc.CalibrationError, match="homography"):
        cc.rewrite_calib("contract_version: '1.5.0'\n", np.eye(3), "2026-10-24T12:00:00+00:00")


# --- the station accepts what this writes -------------------------------------------------


def test_a_station_runs_on_the_written_calibration():
    """The round trip that matters: calibrate -> station.py takes the geometry path.

    Without this, the two halves could each be self-consistent and disagree -- which is how
    a rover ends up uploading keyframes only on demo day for no visible reason.
    """
    import yaml

    from rover.camera import Camera, SyntheticSource
    from rover.station import Station

    src = (_ROOT / "rover" / "calib" / "calib.yaml").read_text(encoding="utf-8")
    H = cc.homography_from_clicks(CLICKS, NEAR, WIDTH, DEPTH, RES, SIZE)
    calib = parse_calib(yaml.safe_load(cc.rewrite_calib(src, H, "2026-10-24T12:00:00+00:00")))

    station = Station(
        calib,
        Camera(calib.camera, SyntheticSource(calib.camera.width, calib.camera.height)),
        None,
        None,
        None,
        store=object(),  # type: ignore[arg-type]
    )
    assert station.geometry_ok is True, "station.py must trust a measured homography"
    station.preflight()  # and the gate that refuses an uncalibrated station now passes
