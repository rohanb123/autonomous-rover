"""Nav camera capture (P2 role doc section 4, watch-outs in section 9).

Two watch-outs are encoded here rather than left as comments:

  * **Stale frames.** A USB webcam buffers; `read()` right after a turn hands back a frame
    from before the turn. `grab()` discards `calib.camera.flush_frames` (3) frames first.
    Without that, station keyframes are attributed to the wrong heading.
  * **Auto-exposure.** It shifts between views, which breaks BEV stitching and visual
    odometry. `open()` tries to lock exposure and white balance, then *reads the property
    back* to see whether the driver accepted it, and records one `exposure_locked` flag.
    That is docs/05 U-21, and it is answerable only on the real hardware.

`cv2` is imported lazily inside the OpenCV source, so this module loads in a CI with no
OpenCV and `rover.main` can still offer `--camera off`.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from rover.config import CameraCfg

log = logging.getLogger("rover.camera")

# V4L2 via OpenCV: 0.25 means "manual", 0.75 means "aperture priority". Driver-dependent,
# which is exactly why open() verifies instead of assuming.
AUTO_EXPOSURE_MANUAL = 0.25


class CameraError(RuntimeError):
    """The camera is absent, or a capture failed."""


class FrameSource(Protocol):
    """The capture surface `Camera` needs."""

    def read(self) -> tuple[bool, Any]: ...
    def set(self, prop: int, value: float) -> bool: ...
    def get(self, prop: int) -> float: ...
    def release(self) -> None: ...


class NullSource:
    """No camera. Every grab fails loudly rather than returning a fake frame."""

    present = False

    def read(self) -> tuple[bool, Any]:
        return False, None

    def set(self, prop: int, value: float) -> bool:
        return False

    def get(self, prop: int) -> float:
        return 0.0

    def release(self) -> None:
        pass


class SyntheticSource:
    """A deterministic regolith-ish frame. For laptop runs and proposal tests, never the Pi."""

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
        # BGR, warmed towards sand so it looks like the real thing in a debug stream.
        self._frame = np.dstack(
            [(gray * 0.72).astype(np.uint8), (gray * 0.86).astype(np.uint8), gray]
        )

    def read(self) -> tuple[bool, Any]:
        return True, self._frame.copy()

    def set(self, prop: int, value: float) -> bool:
        return True

    def get(self, prop: int) -> float:
        return 0.0

    def release(self) -> None:
        pass


class OpenCvSource:
    """A real `cv2.VideoCapture`. Lazy import; opening can block for seconds."""

    present = True

    def __init__(self, index: int, cfg: CameraCfg) -> None:
        try:
            import cv2
        except ImportError as e:
            raise CameraError(f"opencv is not installed: {e}") from e
        self._cv2 = cv2
        self._cap = cv2.VideoCapture(index)
        if not self._cap.isOpened():
            self._cap.release()
            raise CameraError(f"no camera at index {index}")
        # MJPG first: a USB cam at 640x480 is far faster compressed than raw YUYV.
        if cfg.fourcc:
            self._cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*cfg.fourcc))
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
        self._cap.set(cv2.CAP_PROP_FPS, cfg.fps)

    def read(self) -> tuple[bool, Any]:
        return self._cap.read()

    def set(self, prop: int, value: float) -> bool:
        return bool(self._cap.set(prop, value))

    def get(self, prop: int) -> float:
        return float(self._cap.get(prop))

    def release(self) -> None:
        self._cap.release()


class Camera:
    """Grabs frames and encodes JPEGs. `grab()` blocks, so only call it off the loop."""

    def __init__(self, cfg: CameraCfg, source: FrameSource | None = None) -> None:
        self.cfg = cfg
        self._source: FrameSource = source if source is not None else NullSource()
        self.exposure_locked = False
        self.white_balance_locked = False

    @property
    def present(self) -> bool:
        return bool(getattr(self._source, "present", False))

    def open(self, index: int | None = None, w: int | None = None, h: int | None = None) -> None:
        """Open the configured camera and try to lock exposure. Idempotent-ish."""
        if self.present:
            self._lock_exposure()
            return
        cfg = self.cfg
        if w is not None or h is not None:
            cfg = CameraCfg(**{**cfg.__dict__, "width": w or cfg.width, "height": h or cfg.height})
            self.cfg = cfg
        self._source = OpenCvSource(cfg.index if index is None else index, cfg)
        self._lock_exposure()

    def _lock_exposure(self) -> None:
        """docs/05 U-21: attempt the lock, then read it back. Never assume it took."""
        src, cfg = self._source, self.cfg
        try:
            import cv2
        except ImportError:
            return
        if cfg.lock_exposure:
            src.set(cv2.CAP_PROP_AUTO_EXPOSURE, AUTO_EXPOSURE_MANUAL)
            src.set(cv2.CAP_PROP_EXPOSURE, cfg.exposure)
            got = src.get(cv2.CAP_PROP_AUTO_EXPOSURE)
            self.exposure_locked = abs(got - AUTO_EXPOSURE_MANUAL) < 1e-3
            if not self.exposure_locked:
                log.warning(
                    "exposure lock not honoured (AUTO_EXPOSURE reads %.3f); "
                    "falling back to auto with a %.2f s settle (docs/05 U-21)",
                    got,
                    cfg.settle_s,
                )
        if cfg.lock_white_balance:
            self.white_balance_locked = bool(src.set(cv2.CAP_PROP_AUTO_WB, 0))
            if not self.white_balance_locked:
                log.warning("white-balance lock not honoured; stitching may drift")

    def grab(self) -> Any:
        """One fresh frame. Flushes the driver's buffered stale frames first."""
        if not self.present:
            raise CameraError("no camera: start with --camera opencv once one is attached")
        for _ in range(max(0, self.cfg.flush_frames)):
            self._source.read()
        ok, frame = self._source.read()
        if not ok or frame is None:
            raise CameraError("capture failed; the camera may need re-opening")
        return frame

    def jpeg(self, img: Any, quality: int | None = None) -> bytes:
        try:
            import cv2
        except ImportError as e:
            raise CameraError(f"opencv is not installed, cannot encode: {e}") from e
        q = self.cfg.jpeg_quality if quality is None else quality
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), int(q)])
        if not ok:
            raise CameraError("JPEG encoding failed")
        return bytes(buf.tobytes())

    def close(self) -> None:
        self._source.release()
        self._source = NullSource()
