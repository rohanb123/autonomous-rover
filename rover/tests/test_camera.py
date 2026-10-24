"""Tests for rover/rover/camera.py.

The two assertions that earn their keep are the stale-frame flush and the exposure-lock
verification. Both are P2 role doc section 9 watch-outs, and both are the kind of thing that
silently produces wrong *data* rather than an error: keyframes attributed to the heading
before the turn, or a BEV mosaic that will not stitch because the exposure drifted.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402

from rover.camera import (  # noqa: E402
    AUTO_EXPOSURE_MANUAL,
    Camera,
    CameraError,
    NullSource,
    SyntheticSource,
)
from rover.config import parse_calib  # noqa: E402

CFG = parse_calib(helpers.CALIB_DICT).camera


class FakeSource:
    """Counts reads and decides whether it honours a property set, like a real V4L2 driver."""

    present = True

    def __init__(self, *, honour_exposure: bool = True, fail_after: int | None = None) -> None:
        self.reads = 0
        self.sets: list[tuple[int, float]] = []
        self.props: dict[int, float] = {}
        self.honour_exposure = honour_exposure
        self.fail_after = fail_after
        self.released = False

    def read(self) -> tuple[bool, Any]:
        self.reads += 1
        if self.fail_after is not None and self.reads > self.fail_after:
            return False, None
        return True, [[self.reads]]  # a frame stamped with its own sequence number

    def set(self, prop: int, value: float) -> bool:
        self.sets.append((prop, value))
        if not self.honour_exposure:
            return False
        self.props[prop] = value
        return True

    def get(self, prop: int) -> float:
        return self.props.get(prop, 0.0)

    def release(self) -> None:
        self.released = True


# --- presence ------------------------------------------------------------------------------


def test_no_camera_means_present_false_and_a_loud_grab():
    camera = Camera(CFG, NullSource())
    assert camera.present is False
    with pytest.raises(CameraError, match="no camera"):
        camera.grab()


def test_a_default_camera_has_no_source():
    assert Camera(CFG).present is False


# --- the stale-frame flush -----------------------------------------------------------------


def test_grab_flushes_the_stale_frames_first():
    """P2 role doc section 9: USB webcams buffer, so the first read is from before the turn."""
    source = FakeSource()
    camera = Camera(CFG, source)
    frame = camera.grab()
    assert source.reads == CFG.flush_frames + 1 == 4
    assert frame == [[4]], "the returned frame must be the one after the flush"


def test_the_flush_count_comes_from_calib():
    cfg = parse_calib(helpers.calib_dict(camera={"flush_frames": 0})).camera
    source = FakeSource()
    Camera(cfg, source).grab()
    assert source.reads == 1


def test_a_failed_capture_raises_rather_than_returning_junk():
    camera = Camera(CFG, FakeSource(fail_after=CFG.flush_frames))
    with pytest.raises(CameraError, match="capture failed"):
        camera.grab()


# --- the exposure lock (docs/05 U-21) ------------------------------------------------------


def test_a_honoured_exposure_lock_is_recorded():
    pytest.importorskip("cv2")
    import cv2

    source = FakeSource(honour_exposure=True)
    camera = Camera(CFG, source)
    camera.open()
    assert camera.exposure_locked is True
    assert (cv2.CAP_PROP_AUTO_EXPOSURE, AUTO_EXPOSURE_MANUAL) in source.sets
    assert (cv2.CAP_PROP_EXPOSURE, CFG.exposure) in source.sets
    assert camera.white_balance_locked is True


def test_a_refused_exposure_lock_is_detected_not_assumed():
    """The driver can accept set() and ignore it, so open() reads the property back."""
    pytest.importorskip("cv2")
    camera = Camera(CFG, FakeSource(honour_exposure=False))
    camera.open()
    assert camera.exposure_locked is False
    assert camera.white_balance_locked is False


def test_the_lock_is_skipped_when_calib_says_so():
    pytest.importorskip("cv2")
    cfg = parse_calib(
        helpers.calib_dict(camera={"lock_exposure": False, "lock_white_balance": False})
    ).camera
    source = FakeSource()
    camera = Camera(cfg, source)
    camera.open()
    assert source.sets == [] and camera.exposure_locked is False


# --- encoding ------------------------------------------------------------------------------


def test_the_synthetic_source_is_deterministic_and_the_right_shape():
    a = SyntheticSource(160, 120, seed=3)
    b = SyntheticSource(160, 120, seed=3)
    ok_a, frame_a = a.read()
    ok_b, frame_b = b.read()
    assert ok_a and ok_b
    assert frame_a.shape == (120, 160, 3)
    assert (frame_a == frame_b).all(), "same seed, same frame"
    assert (SyntheticSource(160, 120, seed=4).read()[1] != frame_a).any()


def test_jpeg_round_trips_a_real_frame():
    cv2 = pytest.importorskip("cv2")
    camera = Camera(CFG, SyntheticSource(160, 120))
    blob = camera.jpeg(camera.grab())
    assert blob.startswith(b"\xff\xd8"), "JPEG SOI marker"
    import numpy as np

    decoded = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None and decoded.shape == (120, 160, 3)


def test_close_releases_the_source_and_goes_absent():
    source = FakeSource()
    camera = Camera(CFG, source)
    camera.close()
    assert source.released is True
    assert camera.present is False
