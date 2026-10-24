"""Tests for rover/rover/camera.py -- the CSI (Camera Module 3) and USB paths.

`Picamera2Source` takes its `Picamera2` object by injection, so the whole CSI control dance
is testable on a laptop that has neither picamera2 nor a camera. `FakePicam2` below answers
like the real thing: it reports which controls the sensor has, quantises ExposureTime the way
a real sensor does, and hands back a BGR array.

The assertions that earn their keep:
  * **BGR, not RGB.** picamera2 names its formats in reverse of the numpy channel order, so
    `"RGB888"` must be what we ask for. Getting it wrong swaps red and blue in every keyframe
    and quietly changes every rock's measured contrast.
  * **The exposure lock is verified, not assumed** -- from frame metadata, with a tolerance,
    because a sensor quantises exposure to whole readout lines.
  * **A fixed-focus sensor degrades honestly** instead of crashing on an AfMode it lacks.
  * **The stale-frame flush**, which is what keeps a keyframe attached to the right heading.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402

from rover.camera import (  # noqa: E402
    AF_MODES,
    AUTO_EXPOSURE_MANUAL,
    CSI_FORMAT,
    Camera,
    CameraError,
    NullSource,
    OpenCvSource,
    Picamera2Source,
    SyntheticSource,
    lores_size,
    open_source,
)
from rover.config import parse_calib  # noqa: E402

CFG = parse_calib(helpers.CALIB_DICT).camera

#: Every control a Camera Module 3 has that we touch.
CM3_CONTROLS = (
    "AeEnable",
    "ExposureTime",
    "AnalogueGain",
    "AwbEnable",
    "AfMode",
    "LensPosition",
)
#: A Camera Module 2 has no autofocus.
CM2_CONTROLS = ("AeEnable", "ExposureTime", "AnalogueGain", "AwbEnable")


class FakePicam2:
    """Stands in for `picamera2.Picamera2`, answering like a real sensor."""

    def __init__(
        self,
        controls: tuple[str, ...] = CM3_CONTROLS,
        *,
        exposure_quantum_us: int = 100,
        honour_exposure: bool = True,
        fail_capture: bool = False,
        encoder_fails: bool = False,
    ) -> None:
        self.camera_controls = dict.fromkeys(controls, (0, 0, 0))
        self.applied: dict[str, Any] = {}
        self.configured: dict[str, Any] | None = None
        self.started = False
        self.stopped = False
        self.closed = False
        self.af_cycles = 0
        self._quantum = exposure_quantum_us
        self._honour = honour_exposure
        self._fail = fail_capture
        self._encoder_fails = encoder_fails
        self.encoders: list[tuple[Any, Any, str | None]] = []
        self.encoder_stops = 0

    # --- configuration ---
    def create_video_configuration(
        self, main: dict[str, Any], lores: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        config: dict[str, Any] = {"main": dict(main), "kind": "video"}
        if lores is not None:
            config["lores"] = dict(lores)
        return config

    def create_still_configuration(self, main: dict[str, Any]) -> dict[str, Any]:
        return {"main": dict(main), "kind": "still"}

    def configure(self, config: dict[str, Any]) -> None:
        self.configured = config

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    # --- capture ---
    def capture_array(self, stream: str = "main") -> Any:
        if self._fail:
            raise RuntimeError("sensor timeout")
        import numpy as np

        assert self.configured is not None
        if stream == "lores":
            if "lores" not in self.configured:
                raise RuntimeError("no lores stream configured")
            w, h = self.configured["lores"]["size"]
            # Planar YUV420 is (h * 3/2, w): a full-size Y plane then quarter-size U and V.
            yuv = np.full((h * 3 // 2, w), 128, dtype=np.uint8)
            yuv[:h, :] = 180  # a mid-grey Y plane
            return yuv
        w, h = self.configured["main"]["size"]
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        frame[:, :, 0] = 200  # channel 0 bright: BGR means this is BLUE
        return frame

    def capture_metadata(self) -> dict[str, Any]:
        asked = self.applied.get("ExposureTime")
        if asked is None:
            return {"ExposureTime": 33000}
        if not self._honour:
            return {"ExposureTime": 33000}  # the sensor ignored us and stayed on auto
        # Real sensors round to whole readout lines.
        return {"ExposureTime": (int(asked) // self._quantum) * self._quantum}

    # --- controls ---
    def set_controls(self, controls: dict[str, Any]) -> None:
        unknown = set(controls) - set(self.camera_controls)
        if unknown:
            raise RuntimeError(f"unsupported controls {sorted(unknown)}")
        self.applied.update(controls)

    def autofocus_cycle(self) -> bool:
        self.af_cycles += 1
        return True

    # --- encoder ---
    def start_encoder(self, encoder: Any, output: Any, name: str | None = None) -> None:
        if self._encoder_fails:
            raise RuntimeError("no encoder available")
        self.encoders.append((encoder, output, name))

    def stop_encoder(self) -> None:
        self.encoder_stops += 1


def csi(**kwargs) -> tuple[Picamera2Source, FakePicam2]:
    fake = FakePicam2(**kwargs)
    return Picamera2Source(CFG, fake), fake


# --- the CSI path: configuration ----------------------------------------------------------


def test_the_csi_source_asks_for_a_video_configuration_in_bgr_order():
    source, fake = csi()
    assert fake.configured is not None
    assert fake.configured["kind"] == "video", "a still config re-runs sensor setup per capture"
    assert fake.configured["main"]["size"] == (CFG.width, CFG.height)
    # picamera2's format names are the reverse of the numpy channel order, so "RGB888"
    # is how you ask for a BGR array -- which is what OpenCV and the rest expect.
    assert fake.configured["main"]["format"] == CSI_FORMAT == "RGB888"
    assert fake.started is True
    assert source.present is True and source.name == "picamera2"


def test_the_csi_frame_is_bgr_not_rgb():
    """Channel 0 is blue. If this flips, every rock's contrast changes and Mars goes blue."""
    source, _ = csi()
    ok, frame = source.read()
    assert ok
    assert frame.shape == (CFG.height, CFG.width, 3)
    b, g, r = frame[0, 0]
    assert (int(b), int(g), int(r)) == (200, 0, 0)


def test_a_capture_failure_is_reported_not_raised():
    source, _ = csi(fail_capture=True)
    ok, frame = source.read()
    assert ok is False and frame is None


def test_missing_picamera2_says_how_to_install_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "picamera2", None)
    with pytest.raises(CameraError, match="apt"):
        Picamera2Source(CFG)


def test_release_stops_and_closes():
    source, fake = csi()
    source.release()
    assert fake.stopped is True and fake.closed is True


# --- the CSI path: exposure lock (docs/05 U-21) -------------------------------------------


def test_the_exposure_lock_sets_the_libcamera_controls():
    source, fake = csi()
    exposure_locked, wb_locked = source.lock_exposure(CFG)
    assert exposure_locked is True and wb_locked is True
    assert fake.applied["AeEnable"] is False
    assert fake.applied["AwbEnable"] is False
    assert fake.applied["ExposureTime"] == CFG.exposure_us
    assert fake.applied["AnalogueGain"] == CFG.analogue_gain


def test_sensor_quantisation_still_counts_as_locked():
    """8000 us asked, 7900 reported: that is a readout-line boundary, not a failure."""
    source, _ = csi(exposure_quantum_us=500)
    exposure_locked, _ = source.lock_exposure(CFG)
    assert exposure_locked is True


def test_a_sensor_that_ignores_the_lock_is_detected():
    source, _ = csi(honour_exposure=False)
    exposure_locked, wb_locked = source.lock_exposure(CFG)
    assert exposure_locked is False, "the metadata disagreed, so we must not claim a lock"
    assert wb_locked is True  # AwbEnable was still accepted


def test_the_lock_is_skipped_when_calib_says_so():
    cfg = parse_calib(
        helpers.calib_dict(camera={"lock_exposure": False, "lock_white_balance": False})
    ).camera
    fake = FakePicam2()
    source = Picamera2Source(cfg, fake)
    assert source.lock_exposure(cfg) == (False, False)
    assert fake.applied == {}


# --- the CSI path: focus (docs/05 U-7) ----------------------------------------------------


@pytest.mark.parametrize(
    "distance_m, dioptres",
    [(0.15, 1 / 0.15), (0.8, 1.25), (2.0, 0.5), (0.0, 0.0), (-1.0, 0.0)],
    ids=["present", "survey", "far", "infinity", "negative-is-infinity"],
)
def test_manual_focus_converts_metres_to_dioptres(distance_m: float, dioptres: float):
    source, fake = csi()
    assert source.focus(distance_m) is True
    assert fake.applied["LensPosition"] == pytest.approx(dioptres)
    assert fake.applied["AfMode"] == AF_MODES["Manual"], "continuous AF would hunt mid-survey"


def test_focus_none_runs_one_autofocus_sweep():
    source, fake = csi()
    assert source.focus(None) is True
    assert fake.applied["AfMode"] == AF_MODES["Auto"]
    assert fake.af_cycles == 1


def test_a_fixed_focus_sensor_degrades_honestly():
    """A Camera Module 2 has no AfMode. Report False; never crash, never claim focus."""
    source, fake = csi(controls=CM2_CONTROLS)
    assert source.focus(0.15) is False
    assert "LensPosition" not in fake.applied
    # And the exposure lock still works on that sensor.
    assert source.lock_exposure(CFG)[0] is True


def test_unsupported_controls_are_dropped_rather_than_raising():
    source, fake = csi(controls=("AeEnable",))
    source.lock_exposure(CFG)
    assert set(fake.applied) <= {"AeEnable"}


# --- the USB path --------------------------------------------------------------------------


class FakeCap:
    """A `cv2.VideoCapture` stand-in that can accept a property write and ignore it."""

    def __init__(self, *, honour: bool = True) -> None:
        self.props: dict[int, float] = {}
        self.sets: list[tuple[int, float]] = []
        self.honour = honour
        self.released = False
        self.reads = 0

    def isOpened(self) -> bool:  # noqa: N802 - cv2's name
        return True

    def read(self) -> tuple[bool, Any]:
        self.reads += 1
        return True, [[self.reads]]

    def set(self, prop: int, value: float) -> bool:
        self.sets.append((prop, value))
        if not self.honour:
            return False
        self.props[prop] = value
        return True

    def get(self, prop: int) -> float:
        return self.props.get(prop, 0.0)

    def release(self) -> None:
        self.released = True


def test_the_usb_path_locks_exposure_by_reading_the_property_back():
    cv2 = pytest.importorskip("cv2")
    cap = FakeCap(honour=True)
    source = OpenCvSource(0, CFG, cap)
    assert source.lock_exposure(CFG) == (True, True)
    assert (cv2.CAP_PROP_AUTO_EXPOSURE, AUTO_EXPOSURE_MANUAL) in cap.sets
    assert (cv2.CAP_PROP_EXPOSURE, CFG.exposure) in cap.sets


def test_a_usb_driver_that_ignores_the_write_is_detected():
    pytest.importorskip("cv2")
    source = OpenCvSource(0, CFG, FakeCap(honour=False))
    assert source.lock_exposure(CFG) == (False, False)


def test_the_usb_path_cannot_focus():
    pytest.importorskip("cv2")
    source = OpenCvSource(0, CFG, FakeCap())
    assert source.focus(0.15) is False


# --- backend selection ---------------------------------------------------------------------


def test_open_source_honours_an_explicit_backend():
    assert open_source(CFG, "off").name == "none"
    assert open_source(CFG, "sim").name == "synthetic"
    with pytest.raises(CameraError, match="unknown camera backend"):
        open_source(CFG, "teleport")


def test_auto_falls_back_from_csi_to_usb(monkeypatch):
    """On a laptop there is no CSI module, so `auto` must try USB rather than give up."""
    monkeypatch.setitem(sys.modules, "picamera2", None)
    tried: list[str] = []

    def fake_opencv(index, cfg, cap=None):
        tried.append("opencv")
        raise CameraError("no USB camera either")

    monkeypatch.setattr("rover.camera.OpenCvSource", fake_opencv)
    with pytest.raises(CameraError):
        open_source(CFG, "auto")
    assert tried == ["opencv"], "auto must fall through to USB after CSI fails"


# --- Camera ---------------------------------------------------------------------------------


def test_no_camera_means_present_false_and_a_loud_grab():
    camera = Camera(CFG, NullSource())
    assert camera.present is False and camera.backend == "none"
    with pytest.raises(CameraError, match="--camera picamera2"):
        camera.grab()


def test_open_locks_exposure_and_focuses_for_the_survey():
    fake = FakePicam2()
    camera = Camera(CFG)
    camera.open(backend="off")  # start from nothing...
    camera._source = Picamera2Source(CFG, fake)  # ...then inject the CSI source
    camera.lock_exposure()
    camera.focus_for_survey()
    assert camera.exposure_locked and camera.white_balance_locked and camera.focus_locked
    assert fake.applied["LensPosition"] == pytest.approx(1 / CFG.focus_m)
    assert camera.backend == "picamera2"


def test_focus_for_present_is_nearer_than_for_survey():
    """The whole point of CM3 over a fixed-focus webcam: both distances can be sharp."""
    camera = Camera(CFG, Picamera2Source(CFG, FakePicam2()))
    camera.focus_for_survey()
    survey = camera._source.applied["LensPosition"]  # type: ignore[attr-defined]
    camera.focus_for_present()
    present = camera._source.applied["LensPosition"]  # type: ignore[attr-defined]
    assert present > survey, "nearer focus = more dioptres"
    assert present == pytest.approx(1 / CFG.present_focus_m)


def test_grab_flushes_the_frames_already_in_flight():
    pytest.importorskip("cv2")
    cap = FakeCap()
    camera = Camera(CFG, OpenCvSource(0, CFG, cap))
    camera.exposure_locked = True  # skip the settle so this stays fast
    frame = camera.grab()
    assert cap.reads == CFG.flush_frames + 1 == 4
    assert frame == [[4]], "the returned frame must be the one after the flush"


def test_grab_settles_only_when_the_exposure_is_not_locked(monkeypatch):
    """docs/05 U-21's fallback: if we could not pin exposure, give AE time to converge."""
    slept: list[float] = []
    monkeypatch.setattr("rover.camera.time.sleep", slept.append)
    camera = Camera(CFG, Picamera2Source(CFG, FakePicam2()))

    camera.exposure_locked = False
    camera.grab()
    assert slept == [CFG.settle_s]

    slept.clear()
    camera.exposure_locked = True
    camera.grab()
    assert slept == [], "a locked exposure needs no settle"


def test_a_failed_capture_raises_rather_than_returning_junk():
    camera = Camera(CFG, Picamera2Source(CFG, FakePicam2(fail_capture=True)))
    camera.exposure_locked = True
    with pytest.raises(CameraError, match="capture failed"):
        camera.grab()


def test_jpeg_round_trips_a_csi_frame():
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    camera = Camera(CFG, Picamera2Source(CFG, FakePicam2()))
    camera.exposure_locked = True
    blob = camera.jpeg(camera.grab())
    assert blob.startswith(b"\xff\xd8"), "JPEG SOI marker"
    decoded = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None and decoded.shape == (CFG.height, CFG.width, 3)
    # Still blue after the round trip: the channel order survived.
    b, g, r = decoded[0, 0]
    assert int(b) > 150 and int(g) < 80 and int(r) < 80


def test_status_reports_what_was_actually_achieved():
    camera = Camera(CFG, Picamera2Source(CFG, FakePicam2()))
    camera.lock_exposure()
    camera.focus_for_survey()
    status = camera.status()
    assert status["backend"] == "picamera2"
    assert status["size"] == [CFG.width, CFG.height]
    assert status["exposure_locked"] is True and status["focus_locked"] is True


def test_close_releases_and_resets_every_flag():
    fake = FakePicam2()
    camera = Camera(CFG, Picamera2Source(CFG, fake))
    camera.lock_exposure()
    camera.focus_for_survey()
    camera.close()
    assert fake.stopped and fake.closed
    assert camera.present is False and camera.backend == "none"
    assert not camera.exposure_locked and not camera.focus_locked


# --- the synthetic and null sources --------------------------------------------------------


def test_the_synthetic_source_is_deterministic_and_the_right_shape():
    a = SyntheticSource(160, 120, seed=3)
    b = SyntheticSource(160, 120, seed=3)
    ok_a, frame_a = a.read()
    ok_b, frame_b = b.read()
    assert ok_a and ok_b
    assert frame_a.shape == (120, 160, 3)
    assert (frame_a == frame_b).all(), "same seed, same frame"
    assert (SyntheticSource(160, 120, seed=4).read()[1] != frame_a).any()


def test_the_synthetic_source_claims_a_locked_exposure():
    """Not a fiction: a generated frame is fixed-exposure by construction."""
    assert SyntheticSource(64, 48).lock_exposure(CFG) == (True, True)
    assert SyntheticSource(64, 48).focus(0.2) is False


def test_the_null_source_never_pretends():
    null = NullSource()
    assert null.read() == (False, None)
    assert null.lock_exposure(CFG) == (False, False)
    assert null.focus(0.2) is False
    assert null.metadata() == {}


# --- the lores stream (doc 00 section 3, :8080 debug only) ---------------------------------


def test_both_streams_are_declared_in_one_configuration():
    """Picamera2() can only be claimed once, and streams are fixed at configure() time.

    If `lores` is not declared here, a snapshot server has to either open a second camera
    (which fails outright) or steal frames from `main` and stutter every station grab.
    """
    source, fake = csi()
    assert fake.configured is not None
    assert "main" in fake.configured and "lores" in fake.configured
    assert fake.configured["lores"]["size"] == (320, 240)
    # picamera2 refuses anything but YUV420 for lores: it is the ISP's downscale output.
    assert fake.configured["lores"]["format"] == "YUV420"
    assert source.lores == (320, 240)


@pytest.mark.parametrize(
    "main, want_lores, lores_w",
    [
        ((640, 480), (320, 240), 320),  # the default: exactly half
        ((1280, 720), (320, 180), 320),  # 16:9 main -> 16:9 lores, not 4:3
        ((640, 480), (160, 120), 160),
        ((320, 240), (320, 240), 640),  # clamped: lores may not exceed main
        ((641, 481), (320, 240), 321),  # odd numbers rounded down; YUV420 subsamples 2x2
    ],
    ids=["default", "16:9", "small", "clamped", "odd"],
)
def test_lores_is_snapped_to_something_picamera2_accepts(main, want_lores, lores_w):
    cfg = parse_calib(
        helpers.calib_dict(camera={"width": main[0], "height": main[1], "lores_width": lores_w})
    ).camera
    assert lores_size(cfg) == want_lores
    w, h = want_lores
    assert w % 2 == 0 and h % 2 == 0
    assert w <= main[0] and h <= main[1]


def test_read_lores_converts_yuv420_to_bgr():
    pytest.importorskip("cv2")
    source, _ = csi()
    ok, frame = source.read_lores()
    assert ok
    # YUV420 arrives as (h * 3/2, w) planar; BGR comes back as (h, w, 3).
    assert frame.shape == (240, 320, 3)


def test_read_lores_reports_a_failure_rather_than_raising():
    source, _ = csi(fail_capture=True)
    assert source.read_lores() == (False, None)


# --- the hardware MJPEG encoder -----------------------------------------------------------


def test_stream_mjpeg_hands_the_lores_stream_to_the_encoder():
    """The Pi 5 encodes MJPEG in the ISP; a Python loop would cost ~25% CPU instead."""
    source, fake = csi()
    sink = io.BytesIO()
    assert source.stream_mjpeg(sink, encoder=object()) is True
    assert source.streaming is True
    assert len(fake.encoders) == 1
    _encoder, _output, name = fake.encoders[0]
    assert name == "lores", "the browser must not be fed off the survey stream"


def test_starting_the_stream_twice_is_a_noop():
    source, fake = csi()
    assert source.stream_mjpeg(io.BytesIO(), encoder=object()) is True
    assert source.stream_mjpeg(io.BytesIO(), encoder=object()) is True
    assert len(fake.encoders) == 1


def test_an_encoder_that_will_not_start_degrades_to_false():
    """False is the caller's cue to loop read_lores() + jpeg() instead."""
    source, _ = csi(encoder_fails=True)
    assert source.stream_mjpeg(io.BytesIO(), encoder=object()) is False
    assert source.streaming is False


def test_without_picamera2s_encoder_api_the_stream_declines():
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setitem(sys.modules, "picamera2.encoders", None)
        source, fake = csi()
        assert source.stream_mjpeg(io.BytesIO()) is False
        assert fake.encoders == []
    finally:
        monkey.undo()


def test_stop_stream_is_idempotent_and_runs_before_close():
    source, fake = csi()
    source.stream_mjpeg(io.BytesIO(), encoder=object())
    source.stop_stream()
    source.stop_stream()
    assert fake.encoder_stops == 1 and source.streaming is False
    # release() must stop the encoder before closing, or picamera2 complains.
    source.stream_mjpeg(io.BytesIO(), encoder=object())
    source.release()
    assert fake.encoder_stops == 2 and fake.closed is True


# --- the lores stream through Camera ------------------------------------------------------


def test_grab_lores_does_not_flush_or_settle(monkeypatch):
    """A live view wants the newest frame; grab() wants a clean one. Opposite trade-offs."""
    pytest.importorskip("cv2")
    slept: list[float] = []
    monkeypatch.setattr("rover.camera.time.sleep", slept.append)
    camera = Camera(CFG, Picamera2Source(CFG, FakePicam2()))
    camera.exposure_locked = False  # grab() would settle here; grab_lores() must not
    frame = camera.grab_lores()
    assert frame.shape == (240, 320, 3)
    assert slept == []


def test_grab_lores_without_a_camera_is_loud():
    camera = Camera(CFG, NullSource())
    with pytest.raises(CameraError, match="nothing to stream"):
        camera.grab_lores()


def test_the_synthetic_source_streams_so_snapshot_server_can_be_built_on_a_laptop():
    source = SyntheticSource(640, 480)
    ok, small = source.read_lores()
    _, full = source.read()
    assert ok
    assert small.shape[0] < full.shape[0] and small.shape[1] < full.shape[1]
    assert small.shape[2] == 3


def test_a_usb_camera_has_one_stream_so_lores_is_main():
    pytest.importorskip("cv2")
    cap = FakeCap()
    source = OpenCvSource(0, CFG, cap)
    ok_lores, lores = source.read_lores()
    assert ok_lores and cap.reads == 1, "read_lores delegates straight to read"
    ok_main, main = source.read()
    assert ok_main and cap.reads == 2
    assert type(lores) is type(main), "same stream, so the same kind of frame"
    assert source.stream_mjpeg(io.BytesIO()) is False


def test_the_null_source_cannot_stream():
    null = NullSource()
    assert null.read_lores() == (False, None)
    assert null.stream_mjpeg(io.BytesIO()) is False


def test_status_reports_the_lores_size():
    camera = Camera(CFG, Picamera2Source(CFG, FakePicam2()))
    assert camera.status()["lores"] == [320, 240]
    assert Camera(CFG, NullSource()).status()["lores"] is None


def test_closing_the_camera_stops_the_stream():
    fake = FakePicam2()
    camera = Camera(CFG, Picamera2Source(CFG, fake))
    camera.stream_mjpeg(io.BytesIO(), encoder=object())
    camera.close()
    assert fake.encoder_stops >= 1 and fake.closed is True
    assert camera.backend == "none"
