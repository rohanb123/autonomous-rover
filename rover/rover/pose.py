"""Dead-reckoning pose estimate in the mission frame (docs/00 section 4).

Mission frame: origin at the home pad, +x = the heading at run start, +y = left, heading CCW
from +x wrapped to (-180, 180]. All of that arithmetic lives in
`marsrover_shared.frames`, so this module never reimplements it.

The stored `Pose` is immutable and replaced wholesale under a lock: the 5 Hz telemetry loop
reads it from the asyncio thread while a drive updates it from the op worker thread, and
swapping one reference is atomic where mutating three floats is not.

`sigma_xy_m` grows with distance driven and resets on `set_pose`, which is the only thing that
tells the brain how much to trust a target position. Doc 05 R2 is heading drift, and a 5 deg
heading error at 1.5 m is 13 cm -- bigger than the 0.20 m exclusion radius.
"""

from __future__ import annotations

import logging
import threading

from marsrover_shared.frames import rover_to_mission, wrap_deg
from marsrover_shared.schemas import Pose, PoseSource
from rover.config import PoseCfg

log = logging.getLogger("rover.pose")

HOME = Pose(x=0.0, y=0.0, heading_deg=0.0, source="dr", sigma_xy_m=0.0)


class PoseEstimator:
    """Integrates drive/turn commands into a mission-frame pose."""

    def __init__(self, cfg: PoseCfg, pose: Pose | None = None) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._pose = pose or HOME.model_copy(update={"sigma_xy_m": cfg.sigma_xy_m0})
        self._driven_m = 0.0
        self._has_gyro = False

    def current(self) -> Pose:
        with self._lock:
            return self._pose  # immutable, so handing out the reference is safe

    @property
    def source(self) -> PoseSource:
        return "dr+gyro" if self._has_gyro else "dr"

    def use_gyro(self, available: bool) -> None:
        """Flips the reported source once we know whether turns are gyro-closed (docs/05 U-2)."""
        self._has_gyro = available

    def _sigma(self) -> float:
        grown = self.cfg.sigma_xy_m0 + self.cfg.sigma_xy_per_m * self._driven_m
        return min(self.cfg.sigma_xy_max_m, grown)

    def predict_drive(self, dist_m: float) -> Pose:
        """Advance along the current heading. Negative drives backwards."""
        with self._lock:
            p = self._pose
            x, y = rover_to_mission(p, (dist_m, 0.0))
            self._driven_m += abs(dist_m)
            self._pose = p.model_copy(
                update={"x": x, "y": y, "source": self.source, "sigma_xy_m": self._sigma()}
            )
            return self._pose

    def predict_turn(self, dyaw_deg: float) -> Pose:
        """Rotate in place, CCW positive, wrapped to (-180, 180]."""
        with self._lock:
            p = self._pose
            self._pose = p.model_copy(
                update={
                    "heading_deg": wrap_deg(p.heading_deg + dyaw_deg),
                    "source": self.source,
                    "sigma_xy_m": self._sigma(),
                }
            )
            return self._pose

    def set_pose(self, p: Pose) -> None:
        """An external fix (overhead ArUco, or the brain re-zeroing us). Resets the drift."""
        with self._lock:
            self._driven_m = 0.0
            self._pose = p.model_copy(
                update={
                    "heading_deg": wrap_deg(p.heading_deg),
                    "sigma_xy_m": (
                        p.sigma_xy_m if p.sigma_xy_m is not None else self.cfg.sigma_xy_m0
                    ),
                }
            )
            log.info("pose set to (%.3f, %.3f) %.1f deg from %s", p.x, p.y, p.heading_deg, p.source)
