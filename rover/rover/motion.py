"""Timed drive and turn primitives (P2 role doc section 4).

These run on the op worker thread and block, because driving a motor for 1.2 s *is* blocking.
They never touch the asyncio loop.

A running thread cannot be interrupted, so nothing here relies on cancellation. Motion stops
by two means, both of which `Safety` triggers from the loop thread:

  1. `Motors.inhibit()` + `Motors.stop()` cut the hardware immediately, and the latch is what
     stops this loop's next `set()` from undoing it.
  2. the cooperative poll below notices, within one `stop_tick_s` (20 ms), and returns.

So 20 ms is the stop-latency budget, and the pose is integrated from *elapsed* time rather
than the requested distance -- a drive that was cut reports where it actually got to.

`clock` and `sleep` are injected, so `rover/tests/test_motion.py` runs every timing path in
zero real time.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from marsrover_shared.frames import wrap_deg
from marsrover_shared.schemas import ErrCode, Pose
from rover.config import Calib, interp
from rover.motors import Motors
from rover.pose import PoseEstimator

log = logging.getLogger("rover.motion")

#: Ceiling on a gyro-closed turn: if the gyro reads nothing we must not spin forever.
GYRO_TURN_SLACK = 2.5
GYRO_TURN_FLOOR_S = 1.0

#: Both loops below terminate on elapsed time, so a clock that never advances would spin
#: forever. time.monotonic always advances, so this cannot happen on the rover -- but an
#: injected clock paired with the wrong sleep hangs a test with no diagnostic at all, which
#: is a nasty half-hour. Unreachable with any real clock; instant with a frozen one.
STUCK_CLOCK_TICKS = 10_000


class Aborted(RuntimeError):
    """Motion was cut short by a latch or a stop request; carries the contract error code."""

    def __init__(self, code: ErrCode, msg: str = "") -> None:
        super().__init__(f"{code}: {msg}" if msg else code)
        self.code: ErrCode = code
        self.msg = msg


def _stuck_clock_msg() -> str:
    return (
        f"clock did not advance over {STUCK_CLOCK_TICKS} ticks: an injected clock and sleep "
        "that disagree (pass clock.sleep, not a no-op) would do this"
    )


class ImuLike(Protocol):
    """What `Motion` needs from an IMU. Keeps motion.py independent of imu.py."""

    def has_gyro(self) -> bool: ...
    def read(self) -> dict[str, Any]: ...


class SafetyLike(Protocol):
    def latched(self) -> bool: ...


class Motion:
    def __init__(
        self,
        motors: Motors,
        pose: PoseEstimator,
        calib: Calib,
        imu: ImuLike | None = None,
        safety: SafetyLike | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.motors = motors
        self.pose = pose
        self.calib = calib
        self.imu = imu
        self.safety = safety
        self.clock = clock
        self.sleep = sleep
        self._stop = threading.Event()
        self._tick = max(1e-3, calib.safety.stop_tick_s)

    # --- stop plumbing -------------------------------------------------------------------

    def request_stop(self) -> None:
        """Ask the running primitive to return early. Safe from the asyncio loop thread."""
        self._stop.set()

    def clear_stop(self) -> None:
        self._stop.clear()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def _cut(self) -> ErrCode | None:
        """Why we should stop right now, if we should."""
        if self.safety is not None and self.safety.latched():
            return "E_ESTOP"
        if self.motors.inhibited:
            # A latch we did not set: the watchdog or the tilt cutoff got there first.
            return "E_TIMEOUT" if "watchdog" in self.motors.latches else "E_TILT"
        if self._stop.is_set():
            return None  # a plain `stop`: not an error, just a short drive
        return None

    def _run_for(
        self, total_s: float, speed_pair: tuple[float, float]
    ) -> tuple[float, ErrCode | None]:
        """Drive at `speed_pair` for `total_s`. Returns (elapsed, abort code or None)."""
        self.motors.set(*speed_pair)
        t0 = self.clock()
        stuck = 0
        try:
            while True:
                now = self.clock()
                elapsed = now - t0
                if elapsed >= total_s:
                    return total_s, None
                code = self._cut()
                if code is not None:
                    return elapsed, code
                if self._stop.is_set():
                    return elapsed, None
                self.sleep(min(self._tick, total_s - elapsed))
                stuck = 0 if self.clock() > now else stuck + 1
                if stuck > STUCK_CLOCK_TICKS:
                    raise Aborted("E_INTERNAL", _stuck_clock_msg())
        finally:
            self.motors.stop()

    def _speed(self, requested: float) -> float:
        """Clamp to the calibrated usable band. Below min_speed the gearmotors just buzz."""
        return max(self.calib.motion.min_speed, min(1.0, abs(requested)))

    # --- primitives ----------------------------------------------------------------------

    def drive(self, dist_m: float, speed: float) -> Pose:
        """Drive `dist_m` (negative = backwards) at `speed` 0..1. Timed from calib."""
        self.clear_stop()
        duty = self._speed(speed)
        v = interp(self.calib.m_per_s_at, duty)
        if v <= 0:
            raise Aborted("E_INTERNAL", f"calibration says {duty:.2f} speed is 0 m/s")
        total_s = abs(dist_m) / v
        sign = 1.0 if dist_m >= 0 else -1.0
        log.info("drive %.3f m at %.2f (%.2f m/s, %.2f s)", dist_m, duty, v, total_s)
        elapsed, code = self._run_for(total_s, (sign * duty, sign * duty))
        travelled = sign * v * elapsed
        pose = self.pose.predict_drive(travelled)
        if code is not None:
            raise Aborted(code, f"drive cut after {travelled:.3f} m of {dist_m:.3f} m")
        return pose

    def turn(self, deg: float, speed: float) -> tuple[Pose, str]:
        """Turn `deg` (CCW positive) at `speed`. Gyro-closed when there is a gyro."""
        self.clear_stop()
        duty = self._speed(speed)
        sign = 1.0 if deg >= 0 else -1.0
        if self.imu is not None and self.imu.has_gyro():
            pose, code, turned = self._turn_gyro(deg, duty, sign)
            method = "gyro"
        else:
            pose, code, turned = self._turn_timed(deg, duty, sign)
            method = "timed"
        if code is not None:
            raise Aborted(code, f"turn cut after {turned:.1f} deg of {deg:.1f} deg")
        return pose, method

    def _turn_timed(
        self, deg: float, duty: float, sign: float
    ) -> tuple[Pose, ErrCode | None, float]:
        w = interp(self.calib.deg_per_s_at, duty)
        if w <= 0:
            raise Aborted("E_INTERNAL", f"calibration says {duty:.2f} speed is 0 deg/s")
        total_s = abs(deg) / w
        log.info("turn %.1f deg at %.2f (timed, %.1f deg/s, %.2f s)", deg, duty, w, total_s)
        elapsed, code = self._run_for(total_s, (-sign * duty, sign * duty))
        turned = sign * w * elapsed
        return self.pose.predict_turn(turned), code, turned

    def _turn_gyro(
        self, deg: float, duty: float, sign: float
    ) -> tuple[Pose, ErrCode | None, float]:
        """Integrate gz_dps until we have swept |deg|, with a wall-clock ceiling.

        The ceiling matters: a dead or mis-scaled gyro reads ~0 forever, and without it the
        rover would spin until the heartbeat watchdog happened to notice.
        """
        assert self.imu is not None
        nominal = interp(self.calib.deg_per_s_at, duty)
        ceiling = (
            (abs(deg) / nominal) * GYRO_TURN_SLACK + GYRO_TURN_FLOOR_S if nominal > 0 else 10.0
        )
        gain = self.calib.motion.gyro_turn_gain
        log.info("turn %.1f deg at %.2f (gyro, ceiling %.2f s)", deg, duty, ceiling)

        self.motors.set(-sign * duty, sign * duty)
        swept = 0.0
        t0 = last = self.clock()
        code: ErrCode | None = None
        stuck = 0
        try:
            while True:
                now = self.clock()
                dt, last = now - last, now
                swept += abs(float(self.imu.read().get("gz_dps", 0.0))) * dt * gain
                if swept >= abs(deg):
                    break
                if now - t0 >= ceiling:
                    log.warning("gyro turn hit its %.2f s ceiling at %.1f deg", ceiling, swept)
                    break
                code = self._cut()
                if code is not None or self._stop.is_set():
                    break
                self.sleep(self._tick)
                stuck = 0 if self.clock() > now else stuck + 1
                if stuck > STUCK_CLOCK_TICKS:
                    raise Aborted("E_INTERNAL", _stuck_clock_msg())
        finally:
            self.motors.stop()
        turned = sign * min(swept, abs(deg))
        return self.pose.predict_turn(turned), code, turned

    def stop(self) -> Pose:
        """Halt now and report where we are. Safe from the asyncio loop thread."""
        self.request_stop()
        self.motors.stop()
        return self.pose.current()

    def wrapped_delta(self, target_heading_deg: float) -> float:
        """The shortest turn from the current heading to `target_heading_deg`."""
        return wrap_deg(target_heading_deg - self.pose.current().heading_deg)
