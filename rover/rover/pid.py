"""A small PID controller for the motion loops in `rover.motion`.

    pid = Pid(kp, ki, kd, out_min=-0.2, out_max=0.2, i_max=0.1)
    u = pid.update(err, dt, rate=gz_dps)

Two choices worth knowing about:

  * **Derivative on measurement.** Pass `rate` (the gyro's yaw rate, which *is* the
    derivative of the measured angle) and the D term uses `-rate` instead of differencing
    the error. A setpoint never changes mid-loop here, but the gyro rate is a far cleaner
    signal than a finite difference of an integrated angle at 50 Hz.
  * **Anti-windup.** The integral is clamped to `±i_max`, and it stops integrating while
    the output is saturated in the direction the error is pushing. A gearmotor that stalls
    on a rug would otherwise wind the I term up and lurch when it breaks free.

Pure Python, no clock: the caller passes `dt`, so tests drive it with any timeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Pid:
    kp: float
    ki: float = 0.0
    kd: float = 0.0
    out_min: float = float("-inf")
    out_max: float = float("inf")
    i_max: float = float("inf")
    _integral: float = field(default=0.0, init=False, repr=False)
    _last_err: float | None = field(default=None, init=False, repr=False)

    def reset(self) -> None:
        self._integral = 0.0
        self._last_err = None

    def update(self, err: float, dt: float, rate: float | None = None) -> float:
        """One step. `rate` is d(measurement)/dt; without it D falls back to d(err)/dt."""
        if dt <= 0:
            dt = 0.0
        if rate is not None:
            deriv = -rate
        elif self._last_err is not None and dt > 0:
            deriv = (err - self._last_err) / dt
        else:
            deriv = 0.0
        self._last_err = err

        unsat = self.kp * err + self.ki * self._integral + self.kd * deriv
        saturated_high = unsat >= self.out_max and err > 0
        saturated_low = unsat <= self.out_min and err < 0
        if not (saturated_high or saturated_low):
            self._integral = max(-self.i_max, min(self.i_max, self._integral + err * dt))

        out = self.kp * err + self.ki * self._integral + self.kd * deriv
        return max(self.out_min, min(self.out_max, out))
