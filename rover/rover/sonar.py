"""HC-SR04 forward range, for `Telemetry.range_m` (contract 2.0.0).

Same two-layer shape as `imu.py`, for the same reason: a reading blocks (an HC-SR04 echo can
take up to ~25 ms, and longer when nothing comes back), while `Telemetry` is assembled on the
asyncio loop at 5 Hz and must never block. So `Sonar` blocks and `SonarPoller` keeps a cached
value for the loop thread to read.

**Wiring:** TRIG is a plain 3.3 V output and is fine direct. **ECHO is a 5 V output into a
3.3 V-max pin and MUST go through a divider** (1k from ECHO to the pin, 2k from the pin to
ground, giving 5 x 2/3 = 3.33 V). Straight in, it damages the GPIO. Pins are
`pins.sonar_trig` / `pins.sonar_echo` in calib.yaml (GPIO 23/24 by default).

`None` and `0.0` mean different things on purpose: `None` is "no reading" (no sensor, or the
echo timed out), `0.0` would be "something is touching the sensor". Reporting a timeout as 0
would read as an imminent collision, which is the wrong way to be wrong.

UNVERIFIED: untested against a tape measure. The HC-SR04 also does not see soft or angled
surfaces well, so this is situational awareness and a telemetry field -- not a safety
interlock. Nothing in the runtime brakes on it.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any, Protocol

log = logging.getLogger("rover.sonar")

#: Beyond this the HC-SR04 is unreliable; gpiozero also needs it to scale its 0..1 reading.
MAX_RANGE_M = 4.0

#: A reading at or above the ceiling is how gpiozero reports "no echo came back".
NO_ECHO_EPSILON = 0.02

#: Poll rate for the cache. Telemetry goes out at 5 Hz, so 10 Hz is fresh without busy-waiting.
POLL_HZ = 10.0

#: Consecutive failures before the poller gives up and reports absent.
MAX_FAILURES = 10


class RangeSource(Protocol):
    """What `Sonar` needs of a backend, so the tests do not need a Pi."""

    present: bool

    def read_m(self) -> float | None: ...
    def close(self) -> None: ...


class NoSonar:
    """No sensor attached. Distinct from a sensor that reads nothing."""

    present = False
    name = "none"

    def read_m(self) -> float | None:
        return None

    def close(self) -> None:
        pass


class GpioZeroSonar:
    """A real HC-SR04 via gpiozero's DistanceSensor (lgpio underneath on a Pi 5)."""

    present = True
    name = "hc-sr04"

    def __init__(self, trig: int, echo: int, max_range_m: float = MAX_RANGE_M) -> None:
        self.max_range_m = max_range_m
        try:
            from gpiozero import DistanceSensor
        except ImportError as e:  # rover[pi] extra
            raise SonarUnavailable(f"gpiozero is not installed: {e}") from e
        try:
            self._dev = DistanceSensor(echo=echo, trigger=trig, max_distance=max_range_m)
        except Exception as e:  # pin in use, no pin factory, not a Pi
            raise SonarUnavailable(f"cannot claim the sonar pins {trig}/{echo}: {e}") from e

    def read_m(self) -> float | None:
        try:
            metres = float(self._dev.distance) * self.max_range_m
        except Exception as e:
            log.debug("sonar read failed: %s", e)
            return None
        # gpiozero saturates at max_distance when no echo returns, which is not a measurement.
        if metres >= self.max_range_m - NO_ECHO_EPSILON:
            return None
        return metres

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._dev.close()


class SonarUnavailable(RuntimeError):
    """No usable sonar: gpiozero missing, or the pins cannot be claimed."""


class Sonar:
    """One HC-SR04. Blocking: call it from a worker thread, never the event loop."""

    def __init__(self, source: RangeSource | None = None) -> None:
        self._source: RangeSource = source if source is not None else NoSonar()

    @classmethod
    def open(cls, trig: int, echo: int, max_range_m: float = MAX_RANGE_M) -> Sonar:
        return cls(GpioZeroSonar(trig, echo, max_range_m))

    @property
    def present(self) -> bool:
        return bool(getattr(self._source, "present", False))

    @property
    def name(self) -> str:
        return str(getattr(self._source, "name", "?"))

    def read(self) -> float | None:
        """Metres to the nearest thing ahead, or None for no reading."""
        if not self.present:
            return None
        metres = self._source.read_m()
        if metres is None:
            return None
        if metres < 0 or metres > MAX_RANGE_M:
            log.debug("sonar out of range: %.3f m", metres)
            return None
        return metres

    def close(self) -> None:
        self._source.close()


class SonarPoller:
    """Polls `Sonar` on a daemon thread so the loop thread can read a cached metre value."""

    def __init__(
        self,
        sonar: Sonar,
        hz: float = POLL_HZ,
        *,
        max_failures: int = MAX_FAILURES,
        sleep: Any = time.sleep,
    ) -> None:
        self.sonar = sonar
        self.period = 1.0 / max(0.1, hz)
        self.max_failures = max_failures
        self.sleep = sleep
        self._value: float | None = None
        self._failures = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def present(self) -> bool:
        return self.sonar.present and self._failures < self.max_failures

    def start(self) -> None:
        if self._thread is not None or not self.sonar.present:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sonar-poll", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 1.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout_s)

    def poll_once(self) -> float | None:
        value = self.sonar.read()
        with self._lock:
            if value is None:
                self._failures += 1
            else:
                self._failures = 0
            self._value = value
        return value

    def cached(self) -> float | None:
        with self._lock:
            return self._value

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self.sleep(self.period)
