"""The I2C bus the sensors share.

Extracted from `servos.py` when the scoop was scrapped in contract 2.0.0: the bus outlived
the servos. Only `imu.py` and the calibration tools use it now, but it stays its own module
because the next I2C device (an ADS1115, a magnetometer) should not have to import a driver
to get a bus.

`I2cUnavailable` is the one error: no smbus2, or no `/dev/i2c-N`. Both mean "run
`raspi-config nonint do_i2c 0` and `pip install -e rover[pi]`", and both are expected on a
laptop -- so callers that can degrade gracefully catch it rather than crashing.
"""

from __future__ import annotations

from typing import Protocol

__all__ = ["I2cUnavailable", "I2cBus", "Smbus2Bus"]


class I2cUnavailable(RuntimeError):
    """No usable I2C bus: smbus2 missing, or /dev/i2c-N absent."""


class I2cBus(Protocol):
    """The I2C surface the sensor drivers share."""

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
            raise I2cUnavailable(f"smbus2 is not installed: {e}") from e
        try:
            self._bus = SMBus(bus)
        except OSError as e:  # no /dev/i2c-N: I2C not enabled, or not a Pi
            raise I2cUnavailable(f"cannot open I2C bus {bus}: {e}") from e

    def read8(self, addr: int, reg: int) -> int:
        return int(self._bus.read_byte_data(addr, reg))

    def read_block(self, addr: int, reg: int, length: int) -> list[int]:
        return list(self._bus.read_i2c_block_data(addr, reg, length))

    def write8(self, addr: int, reg: int, value: int) -> None:
        self._bus.write_byte_data(addr, reg, value & 0xFF)

    def close(self) -> None:
        closer = getattr(self._bus, "close", None)
        if callable(closer):
            closer()
