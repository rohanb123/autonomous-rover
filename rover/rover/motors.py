"""L293D drive motors on the Pi's GPIO.

Two layers, so everything worth testing runs on a laptop with no GPIO: `Motors` holds the
trim, clamping, direction logic and the safety latch, and talks to a `PinBackend` that is
either real `gpiozero` pins or a recording stub. `GpioZeroPins` is general (it takes the pins
and their frequencies), so `servos.py` drives its two channels through the same abstraction.

The one detail to not get wrong: **`inhibit()` before `stop()`**. A long op runs on a worker
thread and calls `set()` every few milliseconds; stopping the motors without latching them off
just means the worker re-enables them on its next pass, about 20 ms later. That is docs/05 R14
("E-STOP doesn't stop in time"), and it is why `set()` is a no-op while a latch is held.

L293D notes that differ from the TB6612 this replaced:

  * **No STBY.** The enable pins (EN1/EN2, chip pins 1 and 9) are the equivalent, and they are
    also the PWM inputs. EN low puts a bridge's outputs in high-impedance and the motor
    coasts -- a genuine hardware disable, which is what `stop()` uses.
  * **Logic Vss (pin 16) needs 4.5-7 V**, so it goes to the Pi's 5 V pin, not 3V3. Inputs are
    still fine from 3.3 V GPIO (VIH is 2.3 V). A TB6612 harness reused as-is will half-work.
  * **Internal clamp diodes** -- that is what the "D" suffix means -- so no external flyback.
  * **600 mA per channel continuous** (1.2 A peak). A yellow TT gearmotor stalls near
    1.0-1.6 A, so a jammed wheel will thermally limit the chip rather than burn it. Solder all
    four GND pins (4, 5, 12, 13) to copper; they are the only heat path. This supersedes
    docs/05 U-22, which asked the same question about the TB6612.

Pi 5 resolves docs/05 U-1: `gpiozero` with the `lgpio` pin factory. pigpio has no Pi 5
support (https://github.com/joan2937/pigpio/issues/589).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Mapping
from typing import Protocol

from rover.config import Pins

log = logging.getLogger("rover.motors")

#: The as-built harness, BCM numbering. docs/01 section 4 still describes the TB6612 stack;
#: a request is open to P1. rover/tests/test_config.py compares calib.yaml against this.
DOC01_PINS = {
    "en1": 12,
    "in1": 5,
    "in2": 6,
    "en2": 13,
    "in3": 20,
    "in4": 21,
    "led": 22,
    "sonar_trig": 23,
    "sonar_echo": 24,
}


class MotorsUnavailable(RuntimeError):
    """No usable GPIO backend: not a Pi, gpiozero missing, or the pins are already claimed."""


class PinBackend(Protocol):
    """The whole device surface the GPIO drivers need. Deliberately tiny."""

    present: bool

    def pwm(self, pin: int, duty: float) -> None: ...
    def out(self, pin: int, high: bool) -> None: ...
    def close(self) -> None: ...


class NullPins:
    """Accepts and discards every write. Used when no backend was asked for."""

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
    L293D left both enables at zero. The only fiction is that no current flows.
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
    """Real pins via gpiozero. Imported lazily so this module loads on a laptop.

    `pwm` maps pin -> frequency, because the motors want ~1 kHz and the servos 50 Hz off the
    same abstraction. `Motors` and `GpioServos` each build their own instance over their own
    pins, so nothing has to coordinate a shared claim.
    """

    present = True

    def __init__(self, pwm: Mapping[int, int], out: Iterable[int] = ()) -> None:
        try:
            from gpiozero import DigitalOutputDevice, PWMOutputDevice
        except ImportError as e:  # not installed: rover[pi] extra, docs/00 section 2
            raise MotorsUnavailable(f"gpiozero is not installed: {e}") from e
        self._devices: list[object] = []
        try:
            self._pwm = {pin: PWMOutputDevice(pin, frequency=hz) for pin, hz in pwm.items()}
            self._out = {pin: DigitalOutputDevice(pin) for pin in out}
        except Exception as e:  # BadPinFactory, PinPWMUnsupported, pin already in use...
            self.close()
            raise MotorsUnavailable(f"cannot claim pins {sorted(pwm)} / {sorted(out)}: {e}") from e
        self._devices = [*self._pwm.values(), *self._out.values()]

    def pwm(self, pin: int, duty: float) -> None:
        self._pwm[pin].value = max(0.0, min(1.0, duty))  # type: ignore[attr-defined]

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


def motor_pins(pins: Pins) -> tuple[dict[int, int], tuple[int, ...]]:
    """The pin map for a `GpioZeroPins` that drives the L293D (plus the status LED)."""
    pwm = {pins.en1: pins.pwm_hz, pins.en2: pins.pwm_hz}
    return pwm, (pins.in1, pins.in2, pins.in3, pins.in4, pins.led)


class Motors:
    """Differential drive over an L293D. Speeds are -1..1; positive is forward."""

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
            self._side(p.in1, p.in2, p.en1, left)
            self._side(p.in3, p.in4, p.en2, right)
            self._last = (left, right)

    def _side(self, in_a: int, in_b: int, enable: int, value: float) -> None:
        """L293D with EN high: H/L forward, L/H reverse, equal inputs brake.

        The direction pins are set before the enable rises, so a bridge never spends a PWM
        cycle enabled with an undefined direction.
        """
        self._backend.out(in_a, value > 0)
        self._backend.out(in_b, value < 0)
        self._backend.pwm(enable, abs(value))

    def stop(self) -> None:
        """Enables to zero, so the outputs go high-impedance and the motors coast.

        No sleeps, so this is safe to call from the asyncio loop thread -- which is what lets
        `estop` land while a worker thread is mid-drive. EN low is the L293D's equivalent of
        the TB6612's STBY low: a real hardware disable, not just a zero duty cycle.
        """
        with self._lock:
            p = self.pins
            self._backend.pwm(p.en1, 0.0)
            self._backend.pwm(p.en2, 0.0)
            # Leave the direction pins in a defined, de-energised state.
            for pin in (p.in1, p.in2, p.in3, p.in4):
                self._backend.out(pin, False)
            self._last = (0.0, 0.0)

    def brake(self) -> None:
        """Short-brake instead of coasting: equal inputs with the enables held high.

        Stops faster, but dumps the motor's kinetic energy through the L293D at the moment it
        is least able to take it (600 mA/channel), so `stop()` coasts and this stays opt-in.
        Not used by the runtime yet.
        """
        with self._lock:
            p = self.pins
            for pin in (p.in1, p.in2, p.in3, p.in4):
                self._backend.out(pin, True)
            self._backend.pwm(p.en1, 1.0)
            self._backend.pwm(p.en2, 1.0)
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
