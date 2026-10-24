"""Heartbeat watchdog, E-STOP latch and tilt cutoff (P2 role doc sections 4 and 9).

Three cutoffs, one path to the motors: every one of them calls `Motors.inhibit(reason)` and
then `Motors.stop()`, in that order, on the asyncio loop thread. Both are sub-millisecond GPIO
writes with no sleeps, which is what lets `estop` land while a worker thread is mid-drive.

The latches are independent and named, because they clear on different events:
  * `"estop"`     -> only `clear_estop` releases it
  * `"watchdog"`  -> the *next heartbeat* releases it, so a reconnecting brain just resumes
  * `"tilt"`      -> releases after the accelerometer reads safe for `tilt_clear_ticks` in a
                     row, so one bump on the way over a pebble doesn't latch the run off

`clock` and the IMU read are injected, so `rover/tests/test_safety.py` drives the 20 Hz
decision with a fake clock and zero real sleeps.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from typing import Any

from marsrover_shared.schemas import ErrCode
from rover.motors import Motors

log = logging.getLogger("rover.safety")

HEARTBEAT_TIMEOUT_S = 1.0  # docs/00 section 4
TILT_LIMIT_DEG = 35.0  # P2 role doc section 4


def tilt_deg(imu: dict[str, Any]) -> float:
    """Angle between the measured gravity vector and straight down, in degrees.

    atan2 of (horizontal magnitude, vertical) rather than asin, so an upside-down reading
    (az < 0) comes out near 180 instead of wrapping back into the safe range.
    """
    ax = float(imu.get("ax", 0.0))
    ay = float(imu.get("ay", 0.0))
    az = float(imu.get("az", 0.0))
    return math.degrees(math.atan2(math.hypot(ax, ay), az))


class Safety:
    """Owns the latches. `tick()` is called at 20 Hz by the WS server's watchdog loop."""

    def __init__(
        self,
        motors: Motors,
        moving: Callable[[], bool],
        *,
        clock: Callable[[], float] = time.monotonic,
        imu_read: Callable[[], dict[str, Any] | None] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        heartbeat_timeout_s: float = HEARTBEAT_TIMEOUT_S,
        tilt_limit_deg: float = TILT_LIMIT_DEG,
        tilt_clear_ticks: int = 5,
    ) -> None:
        self.motors = motors
        self.moving = moving
        self.clock = clock
        self.imu_read = imu_read
        self.on_event = on_event or (lambda kind, detail: None)
        self.heartbeat_timeout_s = heartbeat_timeout_s
        self.tilt_limit_deg = tilt_limit_deg
        self.tilt_clear_ticks = tilt_clear_ticks

        self._last_heartbeat = clock()
        self._latched = False
        self._cut: ErrCode | None = None
        self._reason: str | None = None
        self._safe_tilt_ticks = 0
        if imu_read is None:
            log.warning("no IMU reader: the tilt cutoff is disabled")

    # --- heartbeat -----------------------------------------------------------------------

    def on_heartbeat(self) -> None:
        """Doc 00 section 4: the brain sends one every 0.25 s."""
        self._last_heartbeat = self.clock()
        self.motors.release("watchdog")

    @property
    def heartbeat_age_s(self) -> float:
        return self.clock() - self._last_heartbeat

    # --- E-STOP --------------------------------------------------------------------------

    def estop(self) -> None:
        """Latch off and cut power now. Ordering matters: inhibit, *then* stop."""
        self._latched = True
        self.motors.inhibit("estop")
        self.motors.stop()
        self._reason = "estop"

    def clear(self) -> None:
        self._latched = False
        self.motors.release("estop")
        self._cut = None
        self._reason = None

    def latched(self) -> bool:
        return self._latched

    # --- tilt ----------------------------------------------------------------------------

    def tilt_ok(self, imu: dict[str, Any] | None) -> bool:
        """False when the rover is leaning past the limit. True when we cannot tell."""
        if not imu:
            return True  # no IMU: never claim a check we are not running
        magnitude = math.hypot(float(imu.get("ax", 0.0)), float(imu.get("ay", 0.0)))
        if magnitude == 0.0 and float(imu.get("az", 0.0)) == 0.0:
            return True  # all zeros is "no reading", not "perfectly level"
        return tilt_deg(imu) <= self.tilt_limit_deg

    # --- the 20 Hz tick ------------------------------------------------------------------

    def tick(self) -> None:
        """Run both automatic cutoffs once. Records a cut for `take_cut()` to hand over."""
        self._tick_heartbeat()
        self._tick_tilt()

    def _tick_heartbeat(self) -> None:
        if not self.moving():
            return
        if "watchdog" in self.motors.latches:
            return  # already cut; the next heartbeat releases it
        if self.heartbeat_age_s <= self.heartbeat_timeout_s:
            return
        age = self.heartbeat_age_s
        log.warning("heartbeat watchdog: no beat for %.2f s while moving", age)
        self.motors.inhibit("watchdog")
        self.motors.stop()
        self.on_event("stall", {"reason": "heartbeat_watchdog", "heartbeat_age_s": round(age, 3)})
        # Doc 00 names no code for this. E_TIMEOUT tells the brain the motion did not finish,
        # instead of letting it believe a `done` it never earned.
        self._cut, self._reason = "E_TIMEOUT", "heartbeat_watchdog"

    def _tick_tilt(self) -> None:
        if self.imu_read is None:
            return
        imu = self.imu_read()
        if self.tilt_ok(imu):
            if "tilt" in self.motors.latches:
                self._safe_tilt_ticks += 1
                if self._safe_tilt_ticks >= self.tilt_clear_ticks:
                    self.motors.release("tilt")
                    self._safe_tilt_ticks = 0
            return
        self._safe_tilt_ticks = 0
        if "tilt" in self.motors.latches:
            return  # already cut, and still tilted
        degrees_now = tilt_deg(imu or {})
        log.warning("tilt cutoff: %.1f deg exceeds %.1f", degrees_now, self.tilt_limit_deg)
        self.motors.inhibit("tilt")
        self.motors.stop()
        self.on_event("tilt", {"tilt_deg": round(degrees_now, 2), "limit": self.tilt_limit_deg})
        self._cut, self._reason = "E_TILT", "tilt"

    # --- handover ------------------------------------------------------------------------

    def take_cut(self) -> ErrCode | None:
        """The code of a cut that just happened, once. The WS server turns it into an abort."""
        code, self._cut = self._cut, None
        return code

    def stop_reason(self) -> str | None:
        return self._reason
