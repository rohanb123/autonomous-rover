"""Nav camera capture: Raspberry Pi Camera Module 3 over CSI, or a USB webcam.

The rover's nav camera is a **Camera Module 3** on the CSI ribbon. That is not a drop-in for
a USB webcam: `cv2.VideoCapture` cannot open a CSI camera on a Pi 5, because the legacy
`bcm2835-v4l2` shim that used to expose it as `/dev/video0` does not exist there. The
supported path is `picamera2`, so `Picamera2Source` is the real backend and `OpenCvSource`
stays for the GX10's USB overhead camera and for laptop work.

`picamera2` is **apt-only** (`sudo apt install -y python3-picamera2`); a venv needs
`--system-site-packages` to see it. Both backends are imported lazily, so this module loads
on a laptop with neither installed and `rover.main --camera off` still works.

Everything the camera is required for, and where it is handled:

  * **Station survey** (8 keyframes, ground 0.3-1.5 m) -- `grab()` after `lock_exposure()` and
    `focus_for_survey()`. Exposure and white balance must be *locked*, or they drift between
    views and the BEV mosaic will not stitch and the VO yaw goes wrong.
  * **Rock proposals** -- same frames. Auto-exposure makes a rock's contrast depend on which
    way the rover happened to be facing, which is exactly the signal `proposals.py` keys on.
  * **PRESENT pose** (a pebble ~15 cm from the lens) -- `focus_for_present()`. The Module 3 is
    the first Pi camera with autofocus, so the survey and this close-up can both be sharp.
    A fixed-focus webcam could only ever have one of them; that is docs/05 U-7.
  * **Approach visual servo** -- repeated `grab()`, which is why the CSI path uses a *video*
    configuration rather than a still one.
  * **Debug snapshot / live stream** on :8080 (doc 00 section 3, debug only) -- the `lores`
    stream, via `read_lores()` or `stream_mjpeg()`. It is a *second* stream off the same
    camera claim: `Picamera2()` can only be held once, so a snapshot server cannot open its
    own, and serving the browser off `main` would stutter every time a station grabs.
  * **Analysis upload** -- `jpeg()`.

docs/05 U-21 ("does the webcam support manual exposure?") resolves **positively** on this
hardware: `AeEnable=False` is a real libcamera control rather than a driver-dependent V4L2
property, and `capture_metadata()` reports what the sensor actually used, so the lock is
verified rather than assumed.
"""

from __future__ import annotations

import contextlib
import logging
import time
from dataclasses import replace
from typing import Any, Protocol

from rover.config import CameraCfg

log = logging.getLogger("rover.camera")

# V4L2 via OpenCV: 0.25 means "manual", 0.75 "aperture priority". Driver-dependent, which is
# why the USB path verifies by reading the property back instead of trusting the write.
AUTO_EXPOSURE_MANUAL = 0.25

# picamera2 names its 3-byte formats in the reverse of the numpy channel order, so "RGB888"
# hands back a BGR array -- which is what OpenCV, bev.py, proposals.py and jpeg() all want.
# Asking for "BGR888" here would silently swap red and blue in every keyframe.
CSI_FORMAT = "RGB888"

# libcamera AfMode values, as a fallback when `from libcamera import controls` is unavailable.
AF_MODES = {"Manual": 0, "Auto": 1, "Continuous": 2}

# picamera2 requires the lores stream to be YUV420 -- it is the ISP's hardware-downscaled
# output, and it will refuse any other format.
LORES_FORMAT = "YUV420"


class CameraError(RuntimeError):
    """The camera is absent, cannot be configured, or a capture failed."""


class FrameSource(Protocol):
    """One capture backend.

    `lock_exposure` and `focus` live here rather than in `Camera` because the CSI and V4L2
    ways of doing them have nothing in common: one sets libcamera controls and reads frame
    metadata, the other pokes `VideoCapture` properties and reads them back. Both return
    what they actually achieved, never what they were asked for.
    """

    name: str
    present: bool

    def read(self) -> tuple[bool, Any]: ...
    def read_lores(self) -> tuple[bool, Any]: ...
    def lock_exposure(self, cfg: CameraCfg) -> tuple[bool, bool]: ...
    def focus(self, distance_m: float | None) -> bool: ...
    def metadata(self) -> dict[str, Any]: ...
    def stream_mjpeg(self, output: Any, encoder: Any = None) -> bool: ...
    def stop_stream(self) -> None: ...
    def release(self) -> None: ...


def lores_size(cfg: CameraCfg) -> tuple[int, int]:
    """The lores dimensions picamera2 will actually accept.

    It must be no larger than `main`, even-sized (YUV420 is subsampled 2x2), and sharing
    main's aspect ratio or the ISP letterboxes it. Fixing that here beats a configure()
    failure on the Pi, so only the width is configurable and the height follows.
    """
    w = min(max(2, cfg.lores_width), cfg.width)
    h = round(w * cfg.height / cfg.width) if cfg.width else w
    return max(2, (w // 2) * 2), max(2, (h // 2) * 2)


class NullSource:
    """No camera. Every grab fails loudly rather than returning a plausible fake frame."""

    name = "none"
    present = False

    def read(self) -> tuple[bool, Any]:
        return False, None

    def read_lores(self) -> tuple[bool, Any]:
        return False, None

    def lock_exposure(self, cfg: CameraCfg) -> tuple[bool, bool]:
        return False, False

    def focus(self, distance_m: float | None) -> bool:
        return False

    def metadata(self) -> dict[str, Any]:
        return {}

    def stream_mjpeg(self, output: Any, encoder: Any = None) -> bool:
        return False

    def stop_stream(self) -> None:
        pass

    def release(self) -> None:
        pass


class SyntheticSource:
    """A deterministic regolith-ish frame. For laptop runs and proposal tests, never the Pi."""

    name = "synthetic"
    present = True

    def __init__(self, width: int = 640, height: int = 480, seed: int = 0) -> None:
        import numpy as np

        rng = np.random.default_rng(seed)
        # A vertical brightness gradient (near ground is brighter) plus grain, then a few
        # dark blobs so proposals.py has something to find at I2.
        base = np.linspace(90, 190, height, dtype=np.float32)[:, None]
        frame = base + rng.normal(0.0, 6.0, size=(height, width)).astype(np.float32)
        for cx, cy, r in ((160, 330, 26), (420, 300, 18), (300, 400, 34)):
            ys, xs = np.ogrid[:height, :width]
            frame[(xs - cx) ** 2 + (ys - cy) ** 2 <= r * r] *= 0.35
        gray = np.clip(frame, 0, 255).astype(np.uint8)
        # BGR, warmed towards sand so a debug stream looks like the real thing.
        self._frame = np.dstack(
            [(gray * 0.72).astype(np.uint8), (gray * 0.86).astype(np.uint8), gray]
        )

    def read(self) -> tuple[bool, Any]:
        return True, self._frame.copy()

    def read_lores(self) -> tuple[bool, Any]:
        """A genuinely smaller frame, so snapshot_server.py can be built on a laptop."""
        h, w = self._frame.shape[:2]
        return True, self._frame[:: max(1, h // 240), :: max(1, w // 320)].copy()

    def lock_exposure(self, cfg: CameraCfg) -> tuple[bool, bool]:
        # Not a fiction: a generated frame is fixed-exposure and fixed-white-balance by
        # construction, which is the property the survey actually depends on.
        return True, True

    def focus(self, distance_m: float | None) -> bool:
        return False  # nothing to focus

    def metadata(self) -> dict[str, Any]:
        return {"Synthetic": True}

    def stream_mjpeg(self, output: Any, encoder: Any = None) -> bool:
        return False  # no hardware encoder; the caller loops read_lores() + jpeg()

    def stop_stream(self) -> None:
        pass

    def release(self) -> None:
        pass


class Picamera2Source:
    """Camera Module 3 over CSI, via picamera2. The rover's real nav camera."""

    name = "picamera2"
    present = True

    def __init__(self, cfg: CameraCfg, picam2: Any = None) -> None:
        self.cfg = cfg
        self.applied: dict[str, Any] = {}
        self.lores = lores_size(cfg)
        self.streaming = False
        self._cam = picam2 if picam2 is not None else self._construct()
        self._start()

    @staticmethod
    def _construct() -> Any:
        try:
            from picamera2 import Picamera2
        except ImportError as e:
            raise CameraError(
                "picamera2 is not installed. It is apt-only: "
                "`sudo apt install -y python3-picamera2`, and the venv needs "
                "--system-site-packages to see it"
            ) from e
        try:
            return Picamera2()
        except Exception as e:  # no ribbon, not enabled, already claimed
            raise CameraError(f"no CSI camera: {e}") from e

    def _start(self) -> None:
        cfg = self.cfg
        try:
            # A *video* configuration: the Module 3 sensor is 11.9 MP and a still
            # configuration re-runs sensor setup on every capture, which would blow the
            # 60 s station budget with 8 keyframes.
            # TWO streams, declared together, because streams are fixed at configure()
            # time and Picamera2() can only be claimed once. `main` is the survey frame;
            # `lores` is the ISP's hardware downscale, for :8080. Without `lores` here, a
            # snapshot server would have to either open a second camera (which fails) or
            # steal frames from `main` and stutter every station grab.
            config = self._cam.create_video_configuration(
                main={"size": (cfg.width, cfg.height), "format": CSI_FORMAT},
                lores={"size": self.lores, "format": LORES_FORMAT},
            )
            self._cam.configure(config)
            self._cam.start()
        except Exception as e:
            self.release()
            raise CameraError(f"cannot start the CSI camera: {e}") from e
        log.info(
            "CSI camera up: main %dx%d %s, lores %dx%d %s",
            cfg.width,
            cfg.height,
            CSI_FORMAT,
            self.lores[0],
            self.lores[1],
            LORES_FORMAT,
        )

    # --- capture --------------------------------------------------------------------------

    def read(self) -> tuple[bool, Any]:
        try:
            frame = self._cam.capture_array("main")
        except Exception as e:
            log.warning("CSI capture failed: %s", e)
            return False, None
        return frame is not None, frame

    def read_lores(self) -> tuple[bool, Any]:
        """The small stream as BGR. It arrives as planar YUV420, so convert it here."""
        try:
            yuv = self._cam.capture_array("lores")
        except Exception as e:
            log.warning("lores capture failed: %s", e)
            return False, None
        if yuv is None:
            return False, None
        try:
            import cv2
        except ImportError:
            return True, yuv  # raw YUV420; the caller can still hand it to an encoder
        code = getattr(cv2, "COLOR_YUV420p2BGR", None)
        if code is None:  # older OpenCV spells it differently
            code = cv2.COLOR_YUV2BGR_I420
        try:
            return True, cv2.cvtColor(yuv, code)
        except Exception as e:
            log.warning("lores YUV420 -> BGR failed: %s", e)
            return False, None

    def metadata(self) -> dict[str, Any]:
        with contextlib.suppress(Exception):
            return dict(self._cam.capture_metadata())
        return {}

    def stream_mjpeg(self, output: Any, encoder: Any = None) -> bool:
        """Hand `lores` to the ISP's MJPEG encoder, writing into `output`.

        Worth the wiring: the Pi 5 encodes MJPEG in hardware, so a 320x240 stream costs a
        couple of percent CPU where a Python read-encode-yield loop costs more like 25% --
        and the rover runtime needs that headroom for stations. Returns False when the
        encoder API is not available, so a caller can fall back to read_lores() + jpeg().

        `encoder` is injectable because MJPEGEncoder's quality knob differs between
        picamera2 releases; snapshot_server.py can pass a configured one. UNVERIFIED: not
        yet exercised against real picamera2 hardware.
        """
        if self.streaming:
            return True
        if encoder is None:
            try:
                from picamera2.encoders import MJPEGEncoder
            except ImportError:
                return False
            encoder = MJPEGEncoder()
        try:
            from picamera2.outputs import FileOutput

            target = FileOutput(output)
        except ImportError:
            target = output
        try:
            self._cam.start_encoder(encoder, target, name="lores")
        except Exception as e:
            log.warning("could not start the MJPEG encoder: %s", e)
            return False
        self.streaming = True
        log.info("MJPEG stream up on the lores stream (%dx%d)", *self.lores)
        return True

    def stop_stream(self) -> None:
        if not self.streaming:
            return
        with contextlib.suppress(Exception):
            self._cam.stop_encoder()
        self.streaming = False

    # --- controls -------------------------------------------------------------------------

    def _supported(self, name: str) -> bool:
        """Not every sensor has every control: a Module 2 has no AfMode at all."""
        with contextlib.suppress(Exception):
            return name in self._cam.camera_controls
        return False

    def _apply(self, controls: dict[str, Any]) -> dict[str, Any]:
        """Set the controls this sensor actually has, and say which were dropped."""
        usable = {k: v for k, v in controls.items() if self._supported(k)}
        dropped = sorted(set(controls) - set(usable))
        if dropped:
            log.warning("this camera has no %s; leaving that on auto", ", ".join(dropped))
        if not usable:
            return {}
        try:
            self._cam.set_controls(usable)
        except Exception as e:
            log.warning("set_controls%s failed: %s", sorted(usable), e)
            return {}
        self.applied.update(usable)
        return usable

    def lock_exposure(self, cfg: CameraCfg) -> tuple[bool, bool]:
        """Pin exposure and white balance, then confirm it from the frame metadata."""
        wanted: dict[str, Any] = {}
        if cfg.lock_exposure:
            wanted["AeEnable"] = False
            wanted["ExposureTime"] = int(cfg.exposure_us)
            wanted["AnalogueGain"] = float(cfg.analogue_gain)
        if cfg.lock_white_balance:
            wanted["AwbEnable"] = False
        if not wanted:
            return False, False
        applied = self._apply(wanted)

        exposure_locked = False
        if cfg.lock_exposure and "ExposureTime" in applied:
            got = self.metadata().get("ExposureTime")
            # The sensor quantises exposure to whole readout lines, so it will not hand back
            # exactly what we asked for. 10% (or 50 us) is well inside "locked".
            tolerance = max(50.0, 0.10 * cfg.exposure_us)
            exposure_locked = got is not None and abs(float(got) - cfg.exposure_us) <= tolerance
            if not exposure_locked:
                log.warning(
                    "exposure lock not honoured: asked %d us, sensor reports %s. "
                    "Falling back to a %.2f s settle before each capture (docs/05 U-21)",
                    cfg.exposure_us,
                    got,
                    cfg.settle_s,
                )
        wb_locked = cfg.lock_white_balance and "AwbEnable" in applied
        if cfg.lock_white_balance and not wb_locked:
            log.warning("white-balance lock not honoured; the BEV mosaic may not stitch")
        return exposure_locked, wb_locked

    def focus(self, distance_m: float | None) -> bool:
        """Focus at `distance_m`, or `None` for one autofocus sweep.

        `LensPosition` is in dioptres (1/metres), 0.0 being infinity. Manual is the right
        mode for the survey: continuous AF would hunt between the 8 views and change the
        image under the BEV stitcher.
        """
        if not self._supported("AfMode"):
            log.info("this camera is fixed-focus; ignoring a focus request")
            return False
        if distance_m is None:
            if not self._apply({"AfMode": self._af_mode("Auto")}):
                return False
            with contextlib.suppress(Exception):
                self._cam.autofocus_cycle()  # blocking sweep, then it holds
            return True
        dioptres = 0.0 if distance_m <= 0 else 1.0 / float(distance_m)
        applied = self._apply({"AfMode": self._af_mode("Manual"), "LensPosition": dioptres})
        if "LensPosition" in applied:
            log.info("focus set to %.2f m (%.2f dioptres)", distance_m, dioptres)
        return "LensPosition" in applied

    @staticmethod
    def _af_mode(name: str) -> Any:
        """The libcamera enum when it is importable, else the documented integer."""
        with contextlib.suppress(ImportError, AttributeError):
            from libcamera import controls as libcontrols

            return getattr(libcontrols.AfModeEnum, name)
        return AF_MODES[name]

    def release(self) -> None:
        cam = getattr(self, "_cam", None)
        if cam is None:
            return
        self.stop_stream()  # stop the encoder before the camera, or close() complains
        for method in ("stop", "close"):
            with contextlib.suppress(Exception):
                getattr(cam, method)()


class OpenCvSource:
    """A USB camera via `cv2.VideoCapture`. The GX10's overhead camera, and laptop work.

    Not usable for the rover's CSI module on a Pi 5 -- see the module docstring.
    """

    name = "opencv"
    present = True

    def __init__(self, index: int, cfg: CameraCfg, cap: Any = None) -> None:
        self.cfg = cfg
        self._cv2 = self._import_cv2()
        self._cap = cap if cap is not None else self._open(index, cfg)

    @staticmethod
    def _import_cv2() -> Any:
        try:
            import cv2
        except ImportError as e:
            raise CameraError(f"opencv is not installed: {e}") from e
        return cv2

    def _open(self, index: int, cfg: CameraCfg) -> Any:
        cv2 = self._cv2
        cap = cv2.VideoCapture(index)
        if not cap.isOpened():
            cap.release()
            raise CameraError(f"no USB camera at index {index}")
        # MJPG first: a USB cam at 640x480 is far faster compressed than raw YUYV.
        if cfg.fourcc:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*cfg.fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        cap.set(cv2.CAP_PROP_FPS, cfg.fps)
        return cap

    def read(self) -> tuple[bool, Any]:
        return self._cap.read()

    def lock_exposure(self, cfg: CameraCfg) -> tuple[bool, bool]:
        """V4L2 lets a driver accept a property write and ignore it, so read it back."""
        cv2 = self._cv2
        exposure_locked = False
        if cfg.lock_exposure:
            self._cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, AUTO_EXPOSURE_MANUAL)
            self._cap.set(cv2.CAP_PROP_EXPOSURE, cfg.exposure)
            got = float(self._cap.get(cv2.CAP_PROP_AUTO_EXPOSURE))
            exposure_locked = abs(got - AUTO_EXPOSURE_MANUAL) < 1e-3
            if not exposure_locked:
                log.warning(
                    "exposure lock not honoured (AUTO_EXPOSURE reads %.3f); falling back to "
                    "a %.2f s settle before each capture (docs/05 U-21)",
                    got,
                    cfg.settle_s,
                )
        wb_locked = False
        if cfg.lock_white_balance:
            wb_locked = bool(self._cap.set(cv2.CAP_PROP_AUTO_WB, 0))
            if not wb_locked:
                log.warning("white-balance lock not honoured; stitching may drift")
        return exposure_locked, wb_locked

    def focus(self, distance_m: float | None) -> bool:
        return False  # a USB webcam is fixed-focus or autofocuses on its own terms

    def read_lores(self) -> tuple[bool, Any]:
        """A USB camera has one stream, so this *is* main. The caller downscales if it cares."""
        return self.read()

    def metadata(self) -> dict[str, Any]:
        return {}

    def stream_mjpeg(self, output: Any, encoder: Any = None) -> bool:
        return False  # no ISP encoder to hand it to

    def stop_stream(self) -> None:
        pass

    def release(self) -> None:
        with contextlib.suppress(Exception):
            self._cap.release()


def open_source(cfg: CameraCfg, backend: str = "auto", index: int | None = None) -> FrameSource:
    """Build a capture backend. `auto` prefers the CSI module, then a USB camera."""
    idx = cfg.index if index is None else index
    if backend in ("off", "none"):
        return NullSource()
    if backend == "sim":
        return SyntheticSource(cfg.width, cfg.height)
    if backend == "picamera2":
        return Picamera2Source(cfg)
    if backend == "opencv":
        return OpenCvSource(idx, cfg)
    if backend != "auto":
        raise CameraError(f"unknown camera backend {backend!r}")
    # The rover has a CSI module; the GX10's overhead camera is USB. Try CSI first.
    try:
        return Picamera2Source(cfg)
    except CameraError as e:
        log.info("no CSI camera (%s); trying USB", e)
    return OpenCvSource(idx, cfg)


class Camera:
    """Grabs frames and encodes JPEGs. `grab()` blocks, so only ever call it off the loop."""

    def __init__(self, cfg: CameraCfg, source: FrameSource | None = None) -> None:
        self.cfg = cfg
        self._source: FrameSource = source if source is not None else NullSource()
        self.exposure_locked = False
        self.white_balance_locked = False
        self.focus_locked = False

    @property
    def present(self) -> bool:
        return bool(getattr(self._source, "present", False))

    @property
    def backend(self) -> str:
        return str(getattr(self._source, "name", "none"))

    def open(
        self,
        index: int | None = None,
        w: int | None = None,
        h: int | None = None,
        backend: str = "auto",
    ) -> None:
        """Open the camera, lock exposure, and focus for the survey."""
        if w is not None or h is not None:
            self.cfg = replace(self.cfg, width=w or self.cfg.width, height=h or self.cfg.height)
        if not self.present:
            self._source = open_source(self.cfg, backend, index)
        self.lock_exposure()
        self.focus_for_survey()

    def lock_exposure(self) -> tuple[bool, bool]:
        """Pin exposure and white balance. Records what was actually achieved."""
        self.exposure_locked, self.white_balance_locked = self._source.lock_exposure(self.cfg)
        return self.exposure_locked, self.white_balance_locked

    # --- focus ---------------------------------------------------------------------------

    def focus(self, distance_m: float | None = None) -> bool:
        self.focus_locked = self._source.focus(distance_m)
        return self.focus_locked

    def focus_for_survey(self) -> bool:
        """Ground 0.3-1.5 m out, for keyframes, the BEV warp and rock proposals."""
        return self.focus(self.cfg.focus_m)

    def focus_for_present(self) -> bool:
        """The pebble in the scoop, ~15 cm from the lens (docs/05 U-7, docs/01 6.2 PRESENT)."""
        return self.focus(self.cfg.present_focus_m)

    # --- capture -------------------------------------------------------------------------

    def grab(self) -> Any:
        """One fresh frame, BGR. Discards the frames already in flight first."""
        if not self.present:
            raise CameraError(
                "no camera: on the Pi use --camera picamera2 (CSI), or --camera sim on a laptop"
            )
        if not self.exposure_locked and self.cfg.settle_s > 0:
            # Auto-exposure needs time to converge after the rover turns, or the keyframe
            # comes out of a different exposure than its neighbours (docs/05 U-21 default).
            time.sleep(self.cfg.settle_s)
        for _ in range(max(0, self.cfg.flush_frames)):
            self._source.read()
        ok, frame = self._source.read()
        if not ok or frame is None:
            raise CameraError("capture failed; the camera may need re-opening")
        return frame

    def grab_lores(self) -> Any:
        """A frame off the small stream, BGR. No flush and no settle, deliberately.

        This feeds a *live* view, where the newest frame matters more than a clean one --
        the opposite trade-off from grab(), which must not hand a station a frame from
        before the turn.
        """
        if not self.present:
            raise CameraError("no camera: nothing to stream")
        ok, frame = self._source.read_lores()
        if not ok or frame is None:
            raise CameraError("lores capture failed")
        return frame

    def stream_mjpeg(self, output: Any, encoder: Any = None) -> bool:
        """Start the hardware MJPEG stream. False means "loop grab_lores() + jpeg()"."""
        return self._source.stream_mjpeg(output, encoder)

    def stop_stream(self) -> None:
        self._source.stop_stream()

    def jpeg(self, img: Any, quality: int | None = None) -> bytes:
        cv2 = OpenCvSource._import_cv2()
        q = self.cfg.jpeg_quality if quality is None else quality
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(q)])
        if not ok:
            raise CameraError("JPEG encoding failed")
        return bytes(buf.tobytes())

    def status(self) -> dict[str, Any]:
        """For the startup inventory and the events.jsonl record."""
        return {
            "backend": self.backend,
            "size": [self.cfg.width, self.cfg.height],
            "lores": list(getattr(self._source, "lores", ())) or None,
            "exposure_locked": self.exposure_locked,
            "white_balance_locked": self.white_balance_locked,
            "focus_locked": self.focus_locked,
        }

    def close(self) -> None:
        self.stop_stream()
        self._source.release()
        self._source = NullSource()
        self.exposure_locked = self.white_balance_locked = self.focus_locked = False
