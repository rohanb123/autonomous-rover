"""TB6612FNG drive motors on the Pi's GPIO (docs/01 section 4 pinout).

Two layers, so everything worth testing runs on a laptop with no GPIO: `Motors` holds the
trim, clamping, direction logic and the safety latch, and talks to a `PinBackend` that is
either real `gpiozero` pins or a recording stub.

The one detail to not get wrong: **`inhibit()` before `stop()`**. A long op runs on a worker
thread and calls `set()` every few milliseconds; stopping the motors without latching them off
just means the worker re-enables them on its next pass, about 20 ms later. That is docs/05 R14
("E-STOP doesn't stop in time"), and it is why `set()` is a no-op while a latch is held.

Pi 5 resolves docs/05 U-1: `gpiozero` with the `lgpio` pin factory. pigpio has no Pi 5
support (https://github.com/joan2937/pigpio/issues/589).
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

from rover.config import Pins

log = logging.getLogger("rover.motors")

#: docs/01 section 4, BCM. The authority is calib.yaml (which rover/tests/test_config.py
#: compares against doc 01); this is here so a reader doesn't have to open the YAML.
DOC01_PINS = {
    "pwm_a": 12,
    "ain1": 5,
    "ain2": 6,
    "pwm_b": 13,
    "bin1": 20,
    "bin2": 21,
    "stby": 26,
    "led": 22,
}


class MotorsUnavailable(RuntimeError):
    """No usable GPIO backend: not a Pi, gpiozero missing, or the pins are already claimed."""


class PinBackend(Protocol):
    """The whole device surface `Motors` needs. Deliberately tiny."""

    def pwm(self, pin: int, duty: float) -> None: ...
    def out(self, pin: int, high: bool) -> None: ...
    def close(self) -> None: ...


class NullPins:
    """Accepts and discards every write. Used when no motor backend was asked for."""

    present = False

    def pwm(self, pin: int, duty: float) -> None:
        pass

    def out(self, pin: int, high: bool) -> None:
        pass

    def close(self) -> None:
        pass


class RecordingPins:
    """Accepts every write and remembers it. `present` is True, so ops actually run.

    This is what `--motors sim` uses: `Motion.drive` runs its real timing loop, the real
    `Safety` and the real watchdog, and a test can assert the last thing written to the
    TB6612 was PWM 0 with STBY low. The only fiction is that no current flows.
    """

    present = True

    def __init__(self) -> None:
        self.writes: list[tuple[str, int, float | bool]] = []
        self.closed = False

    def pwm(self, pin: int, duty: float) -> None:
        self.writes.append(("pwm", pin, duty))

    def out(self, pin: int, high: bool) -> None:
        self.writes.append(("out", pin, high))

    def close(self) -> None:
        self.closed = True

    def last(self, kind: str, pin: int) -> float | bool | None:
        for k, p, v in reversed(self.writes):
            if k == kind and p == pin:
                return v
        return None

    def clear(self) -> None:
        self.writes.clear()


class GpioZeroPins:
    """Real pins via gpiozero. Imported lazily so this module loads on a laptop."""

    present = True

    def __init__(self, pins: Pins) -> None:
        try:
            from gpiozero import DigitalOutputDevice, PWMOutputDevice
        except ImportError as e:  # not installed: rover[pi] extra, docs/00 section 2
            raise MotorsUnavailable(f"gpiozero is not installed: {e}") from e
        self._devices: list[object] = []
        try:
            self._pwm = {
                p: PWMOutputDevice(p, frequency=pins.pwm_hz) for p in (pins.pwm_a, pins.pwm_b)
            }
            self._out = {
                p: DigitalOutputDevice(p)
                for p in (pins.ain1, pins.ain2, pins.bin1, pins.bin2, pins.stby)
            }
        except Exception as e:  # BadPinFactory, PinPWMUnsupported, pin already in use...
            self.close()
            raise MotorsUnavailable(f"cannot claim the motor pins: {e}") from e
        self._devices = [*self._pwm.values(), *self._out.values()]

    def pwm(self, pin: int, duty: float) -> None:
        self._pwm[pin].value = duty  # type: ignore[attr-defined]

    def out(self, pin: int, high: bool) -> None:
        device = self._out[pin]
        if high:
            device.on()  # type: ignore[attr-defined]
        else:
            device.off()  # type: ignore[attr-defined]

    def close(self) -> None:
        for device in getattr(self, "_devices", []):
            try:
                device.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - shutdown must not raise
                log.debug("closing a pin failed", exc_info=True)
        self._devices = []


class Motors:
    """Differential drive over a TB6612. Speeds are -1..1; positive is forward."""

    def __init__(
        self,
        pins: Pins,
        trim: float = 0.0,
        backend: PinBackend | None = None,
        *,
        invert_left: bool = False,
        invert_right: bool = False,
    ) -> None:
        self.pins = pins
        self.trim = trim
        self.invert_left = invert_left
        self.invert_right = invert_right
        self._backend: PinBackend = backend if backend is not None else NullPins()
        # set() runs on the op worker thread; stop() runs on the asyncio loop thread.
        # gpiozero is not documented thread-safe, so serialise every write.
        self._lock = threading.RLock()
        self._latches: set[str] = set()
        self._last: tuple[float, float] = (0.0, 0.0)

    @property
    def present(self) -> bool:
        return bool(getattr(self._backend, "present", False))

    # --- the safety latch ----------------------------------------------------------------

    @property
    def inhibited(self) -> bool:
        return bool(self._latches)

    @property
    def latches(self) -> frozenset[str]:
        return frozenset(self._latches)

    def inhibit(self, reason: str) -> None:
        """Hold the motors off until every holder releases. Call this *before* stop()."""
        with self._lock:
            if reason not in self._latches:
                log.warning("motors inhibited: %s", reason)
            self._latches.add(reason)

    def release(self, reason: str) -> None:
        with self._lock:
            if reason in self._latches:
                self._latches.discard(reason)
                held = " (still held)" if self._latches else ""
                log.info("motors released: %s%s", reason, held)

    # --- driving -------------------------------------------------------------------------

    def _trimmed(self, left: float, right: float) -> tuple[float, float]:
        """trim > 0 slows the LEFT wheel; trim < 0 slows the right. calibrate_motion.py sets it."""
        if self.trim > 0:
            left *= max(0.0, 1.0 - self.trim)
        elif self.trim < 0:
            right *= max(0.0, 1.0 + self.trim)
        return left, right

    def set(self, left: float, right: float) -> None:
        """Drive the wheels at -1..1. Silently does nothing while a latch is held."""
        left = max(-1.0, min(1.0, left))
        right = max(-1.0, min(1.0, right))
        with self._lock:
            if self._latches:
                return  # the whole point: a worker thread cannot undo a stop
            left, right = self._trimmed(left, right)
            if self.invert_left:
                left = -left
            if self.invert_right:
                right = -right
            p = self.pins
            self._backend.out(p.stby, True)  # TB6612 STBY high = enabled
            self._side(p.ain1, p.ain2, p.pwm_a, left)
            self._side(p.bin1, p.bin2, p.pwm_b, right)
            self._last = (left, right)

    def _side(self, in1: int, in2: int, pwm: int, value: float) -> None:
        """TB6612 truth table: H/L forward, L/H reverse, L/L coast."""
        if value > 0:
            self._backend.out(in1, True)
            self._backend.out(in2, False)
        elif value < 0:
            self._backend.out(in1, False)
            self._backend.out(in2, True)
        else:
            self._backend.out(in1, False)
            self._backend.out(in2, False)
        self._backend.pwm(pwm, abs(value))

    def stop(self) -> None:
        """Short-brake both motors, then standby. No sleeps: safe on the asyncio loop."""
        with self._lock:
            p = self.pins
            self._backend.pwm(p.pwm_a, 0.0)
            self._backend.pwm(p.pwm_b, 0.0)
            # IN1 = IN2 = HIGH is TB6612 short brake, and it holds whatever PWM is doing.
            for pin in (p.ain1, p.ain2, p.bin1, p.bin2):
                self._backend.out(pin, True)
            self._backend.out(p.stby, False)  # LOW = motors off, the safety default
            self._last = (0.0, 0.0)

    @property
    def last(self) -> tuple[float, float]:
        """The most recent commanded (left, right), after trim and inversion."""
        return self._last

    def close(self) -> None:
        with self._lock:
            try:
                self.stop()
            finally:
                self._backend.close()
