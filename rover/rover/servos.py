"""PCA9685 servo driver over I2C @0x40 (docs/01 section 4: ch0 shoulder, ch1 wrist).

docs/05 U-23 asks whether to use Adafruit's Blinka PCA9685 library or plain register writes.
This picks `smbus2`: one fewer dependency, no `board`/`busio` Pi-5 question, and the register
protocol is a dozen lines. The two pieces of arithmetic that could actually be wrong --
`prescale_for` and `us_to_ticks` -- are module-level pure functions with unit tests, so U-23
only ever risks the *transport*. Swapping in a `BlinkaBus` later means implementing `I2cBus`
and touching nothing else.

Never power servos from the Pi's 5 V pin (docs/01 section 4): V+ is the servo pack.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Protocol

log = logging.getLogger("rover.servos")

# PCA9685 registers (datasheet section 7.3).
MODE1 = 0x00
MODE2 = 0x01
PRESCALE = 0xFE
LED0_ON_L = 0x06
ALL_LED_ON_L = 0xFA

MODE1_RESTART = 0x80
MODE1_SLEEP = 0x10
MODE1_AI = 0x20  # register auto-increment
MODE2_OUTDRV = 0x04  # totem-pole outputs, which is what a servo wants

OSC_HZ = 25_000_000  # the internal oscillator the prescaler divides
TICKS = 4096
CHANNELS = 16
FULL_OFF = 0x10  # bit 4 of LEDn_OFF_H


class ServosUnavailable(RuntimeError):
    """No usable I2C bus, or nothing answering at the PCA9685's address."""


class I2cBus(Protocol):
    """The I2C surface the servo and IMU drivers share."""

    def read8(self, addr: int, reg: int) -> int: ...
    def read_block(self, addr: int, reg: int, length: int) -> list[int]: ...
    def write8(self, addr: int, reg: int, value: int) -> None: ...
    def close(self) -> None: ...


class Smbus2Bus:
    """Real I2C via smbus2. Imported lazily so this module loads on a laptop."""

    present = True

    def __init__(self, bus: int = 1) -> None:
        try:
            from smbus2 import SMBus
        except ImportError as e:  # rover[pi] extra, docs/00 section 2
            raise ServosUnavailable(f"smbus2 is not installed: {e}") from e
        try:
            self._bus = SMBus(bus)
        except OSError as e:  # no /dev/i2c-N: I2C not enabled, or not a Pi
            raise ServosUnavailable(f"cannot open I2C bus {bus}: {e}") from e

    def read8(self, addr: int, reg: int) -> int:
        return int(self._bus.read_byte_data(addr, reg))

    def read_block(self, addr: int, reg: int, length: int) -> list[int]:
        return list(self._bus.read_i2c_block_data(addr, reg, length))

    def write8(self, addr: int, reg: int, value: int) -> None:
        self._bus.write_byte_data(addr, reg, value & 0xFF)

    def close(self) -> None:
        with_close = getattr(self._bus, "close", None)
        if callable(with_close):
            with_close()


# --- the arithmetic U-23 must not get wrong ------------------------------------------------


def prescale_for(freq_hz: float) -> int:
    """PCA9685 PRESCALE for a target PWM frequency. 50 Hz -> 121."""
    if freq_hz <= 0:
        raise ValueError(f"freq_hz must be positive, got {freq_hz}")
    value = round(OSC_HZ / (TICKS * float(freq_hz))) - 1
    return max(3, min(255, int(value)))  # datasheet: 3 is the minimum legal prescale


def us_to_ticks(us: float, freq_hz: float) -> int:
    """Pulse width in microseconds -> a 0..4095 off-tick. 1500 us at 50 Hz -> 307."""
    ticks = round(float(us) * TICKS * float(freq_hz) / 1_000_000)
    return max(0, min(TICKS - 1, int(ticks)))


class Servos:
    """Pulse-width control of up to 16 channels. Microseconds in, registers out."""

    def __init__(
        self,
        i2c_addr: int = 0x40,
        freq_hz: int = 50,
        bus: I2cBus | None = None,
        *,
        limits_us: tuple[int, int] = (600, 2400),
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.addr = i2c_addr
        self.freq_hz = freq_hz
        self.limits_us = limits_us
        self._bus = bus
        self._lock = threading.RLock()
        self._sleep = sleep or time.sleep
        self._last_us: dict[int, int] = {}
        if bus is not None:
            self._configure()

    @property
    def present(self) -> bool:
        return self._bus is not None

    def _configure(self) -> None:
        """Set the prescaler, which needs the chip asleep, then wake it."""
        assert self._bus is not None
        try:
            self._bus.write8(self.addr, MODE2, MODE2_OUTDRV)
            mode1 = self._bus.read8(self.addr, MODE1)
            self._bus.write8(self.addr, MODE1, (mode1 & ~MODE1_RESTART) | MODE1_SLEEP)
            self._bus.write8(self.addr, PRESCALE, prescale_for(self.freq_hz))
            self._bus.write8(self.addr, MODE1, (mode1 & ~MODE1_SLEEP) | MODE1_AI)
            self._sleep(0.005)  # datasheet: >= 500 us for the oscillator to settle
            self._bus.write8(self.addr, MODE1, (mode1 & ~MODE1_SLEEP) | MODE1_AI | MODE1_RESTART)
        except OSError as e:
            raise ServosUnavailable(f"no PCA9685 at 0x{self.addr:02x}: {e}") from e
        log.info("PCA9685 at 0x%02x configured for %d Hz", self.addr, self.freq_hz)

    def _check_channel(self, ch: int) -> None:
        if not 0 <= ch < CHANNELS:
            raise ValueError(f"channel {ch} is outside 0..{CHANNELS - 1}")

    def write_us(self, ch: int, us: int) -> None:
        """Hold channel `ch` at `us` microseconds, clamped to the calibrated limits."""
        self._check_channel(ch)
        lo, hi = self.limits_us
        clamped = max(lo, min(hi, int(us)))
        if clamped != int(us):
            log.debug("ch%d: %d us clamped to %d", ch, us, clamped)
        if self._bus is None:
            return
        ticks = us_to_ticks(clamped, self.freq_hz)
        base = LED0_ON_L + 4 * ch
        with self._lock:
            self._bus.write8(self.addr, base + 0, 0x00)  # ON_L: start of the period
            self._bus.write8(self.addr, base + 1, 0x00)  # ON_H
            self._bus.write8(self.addr, base + 2, ticks & 0xFF)  # OFF_L
            self._bus.write8(self.addr, base + 3, (ticks >> 8) & 0x0F)  # OFF_H
            self._last_us[ch] = clamped

    def off(self, ch: int) -> None:
        """Stop driving the channel: the servo goes limp instead of holding position."""
        self._check_channel(ch)
        if self._bus is None:
            return
        with self._lock:
            self._bus.write8(self.addr, LED0_ON_L + 4 * ch + 3, FULL_OFF)
            self._last_us.pop(ch, None)

    def last_us(self, ch: int) -> int | None:
        return self._last_us.get(ch)

    def close(self) -> None:
        """Release every channel, then the bus. A held servo cooks if it is stalled."""
        with self._lock:
            if self._bus is None:
                return
            for ch in list(self._last_us):
                try:
                    self.off(ch)
                except OSError:  # noqa: PERF203 - shutdown must not raise
                    log.debug("releasing ch%d failed", ch, exc_info=True)
            self._bus.close()
            self._bus = None
