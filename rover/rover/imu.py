"""IMU over I2C: an MPU-6050 (accel + gyro) or, as a fallback, an accel-only ADXL345.

This is what resolves docs/05 **U-2** and, through it, docs/05 **R2** -- heading drift, the
highest-scoring risk at 9. With a gyro, `Motion.turn` closes the loop on measured rotation
instead of trusting a timing table; without one it falls back to timed turns, and doc 05's
note that a 5 degree heading error at 1.5 m is 13 cm of target error (bigger than the 0.20 m
exclusion radius) is the reason that matters.

The kit has an MPU-6050, so the gyro branch is live. The ADXL345 path stays because doc 01
section 4 still lists it as the thing a kit might contain, it is 30 lines, and `has_gyro()`
is the only switch anywhere downstream.

Two structural choices worth knowing:

  * **One 14-byte burst read per sample.** Accel, temperature and gyro are contiguous from
    0x3B, so a single transaction gets a coherent sample. Reading accel and gyro separately
    would stitch together two different instants, and the gyro integration in
    `Motion._turn_gyro` is exactly where that would show up.
  * **`Imu` blocks; `ImuPoller` is what the event loop touches.** The driver is only ever
    called from the op worker thread (or the poller's own thread). The 20 Hz safety watchdog
    and the 5 Hz telemetry read a cached dict, because an I2C lockup must not stall the one
    loop that must never stall.

Sign convention: docs/01 section 7 step 5 mounts the IMU flat with its arrow at +x forward,
so +z is up and a CCW turn (doc 00 section 4's positive direction) should read positive
`gz_dps`. UNVERIFIED until someone spins it by hand -- but note `Motion._turn_gyro` integrates
`abs(gz_dps)`, so a swapped sign costs us nothing until `heading.py` lands at I4.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

log = logging.getLogger("rover.imu")

#: Standard gravity. doc 00 section 6.2's telemetry example reads az 9.79, so m/s^2.
G = 9.80665

# --- MPU-6050 (docs/01 section 4: 0x68) ---------------------------------------------------
MPU_ADDR = 0x68
MPU_WHO_AM_I = 0x75
#: The datasheet says 0x68, but the common GY-521 clones answer 0x70/0x72/0x98. Accepting
#: them beats refusing to talk to the board that is actually on the rover.
MPU_WHO_AM_I_VALUES = frozenset({0x68, 0x70, 0x72, 0x98})
MPU_SMPLRT_DIV = 0x19
MPU_CONFIG = 0x1A
MPU_GYRO_CONFIG = 0x1B
MPU_ACCEL_CONFIG = 0x1C
MPU_ACCEL_XOUT_H = 0x3B  # accel 6, temp 2, gyro 6 -- contiguous, so one burst read
MPU_BURST_LEN = 14
MPU_PWR_MGMT_1 = 0x6B

MPU_WAKE_PLL_X_GYRO = 0x01  # datasheet prefers this clock over the internal oscillator
MPU_DLPF_44HZ = 0x03  # docs/01 section 8: the IMU sits near the motors and picks up buzz
MPU_SMPLRT_DIV_125HZ = 0x07  # 1 kHz / (1 + 7)
MPU_RANGE_2G = 0x00  # 16384 LSB/g
MPU_RANGE_250DPS = 0x00  # 131 LSB per deg/s
MPU_ACCEL_LSB_PER_G = 16384.0
MPU_GYRO_LSB_PER_DPS = 131.0

# --- ADXL345 (accel only, 0x53) -----------------------------------------------------------
ADXL_ADDR = 0x53
ADXL_DEVID = 0x00
ADXL_DEVID_VALUE = 0xE5
ADXL_POWER_CTL = 0x2D
ADXL_DATA_FORMAT = 0x31
ADXL_DATAX0 = 0x32
ADXL_BURST_LEN = 6
ADXL_MEASURE = 0x08  # POWER_CTL: leave standby
ADXL_FULL_RES_2G = 0x08  # DATA_FORMAT: full resolution, which is a flat 256 LSB/g
ADXL_LSB_PER_G = 256.0

NONE = "none"


class ImuUnavailable(RuntimeError):
    """No I2C bus, or nothing recognisable at any of the configured addresses."""


class I2cBus(Protocol):
    """The same surface servos.py defines; main.py hands one of those in."""

    def read8(self, addr: int, reg: int) -> int: ...
    def read_block(self, addr: int, reg: int, length: int) -> list[int]: ...
    def write8(self, addr: int, reg: int, value: int) -> None: ...
    def close(self) -> None: ...


def be_signed(hi: int, lo: int) -> int:
    """Two bytes, big-endian, two's complement. The MPU-6050's word order."""
    raw = ((hi & 0xFF) << 8) | (lo & 0xFF)
    return raw - 0x10000 if raw & 0x8000 else raw


def le_signed(lo: int, hi: int) -> int:
    """Two bytes, little-endian, two's complement. The ADXL345's word order."""
    raw = ((hi & 0xFF) << 8) | (lo & 0xFF)
    return raw - 0x10000 if raw & 0x8000 else raw


class _Mpu6050:
    """Accel + gyro. The one that makes closed-loop turns possible."""

    kind = f"mpu6050@0x{MPU_ADDR:02x}"
    has_gyro = True

    def __init__(self, bus: I2cBus, addr: int = MPU_ADDR) -> None:
        self.bus = bus
        self.addr = addr

    @staticmethod
    def identify(bus: I2cBus, addr: int) -> bool:
        try:
            return bus.read8(addr, MPU_WHO_AM_I) in MPU_WHO_AM_I_VALUES
        except OSError:
            return False

    def configure(self) -> None:
        """Wake it, then pick a sample rate and a low-pass filter it can live with."""
        bus, addr = self.bus, self.addr
        bus.write8(addr, MPU_PWR_MGMT_1, MPU_WAKE_PLL_X_GYRO)  # out of sleep
        bus.write8(addr, MPU_SMPLRT_DIV, MPU_SMPLRT_DIV_125HZ)
        # The DLPF is not optional on this rover: docs/01 section 8 warns the IMU picks up
        # motor vibration, and unfiltered noise integrates straight into heading error (R2).
        bus.write8(addr, MPU_CONFIG, MPU_DLPF_44HZ)
        bus.write8(addr, MPU_GYRO_CONFIG, MPU_RANGE_250DPS)
        bus.write8(addr, MPU_ACCEL_CONFIG, MPU_RANGE_2G)
        log.info("MPU-6050 at 0x%02x: 125 Hz, DLPF 44 Hz, +/-2 g, +/-250 deg/s", addr)

    def read(self) -> dict[str, float]:
        """One coherent sample: accel in m/s^2 and gyro Z in deg/s."""
        raw = self.bus.read_block(self.addr, MPU_ACCEL_XOUT_H, MPU_BURST_LEN)
        if len(raw) < MPU_BURST_LEN:
            raise OSError(f"short IMU read: {len(raw)} of {MPU_BURST_LEN} bytes")
        scale = G / MPU_ACCEL_LSB_PER_G
        return {
            "ax": be_signed(raw[0], raw[1]) * scale,
            "ay": be_signed(raw[2], raw[3]) * scale,
            "az": be_signed(raw[4], raw[5]) * scale,
            # raw[6:8] is temperature; we have no use for it yet.
            "gz_dps": be_signed(raw[12], raw[13]) / MPU_GYRO_LSB_PER_DPS,
        }


class _Adxl345:
    """Accel only -- no `gz_dps` key at all, which is what `has_gyro()` is really reporting."""

    kind = f"adxl345@0x{ADXL_ADDR:02x}"
    has_gyro = False

    def __init__(self, bus: I2cBus, addr: int = ADXL_ADDR) -> None:
        self.bus = bus
        self.addr = addr

    @staticmethod
    def identify(bus: I2cBus, addr: int) -> bool:
        try:
            return bus.read8(addr, ADXL_DEVID) == ADXL_DEVID_VALUE
        except OSError:
            return False

    def configure(self) -> None:
        bus, addr = self.bus, self.addr
        bus.write8(addr, ADXL_DATA_FORMAT, ADXL_FULL_RES_2G)
        bus.write8(addr, ADXL_POWER_CTL, ADXL_MEASURE)  # leave standby
        log.warning(
            "ADXL345 at 0x%02x: accelerometer only, so turns stay timed and heading drift "
            "is unmitigated (docs/05 U-2, R2)",
            addr,
        )

    def read(self) -> dict[str, float]:
        raw = self.bus.read_block(self.addr, ADXL_DATAX0, ADXL_BURST_LEN)
        if len(raw) < ADXL_BURST_LEN:
            raise OSError(f"short IMU read: {len(raw)} of {ADXL_BURST_LEN} bytes")
        scale = G / ADXL_LSB_PER_G
        # No gz_dps key. Callers use .get("gz_dps", 0.0) and Motion falls back to a timed turn.
        return {
            "ax": le_signed(raw[0], raw[1]) * scale,
            "ay": le_signed(raw[2], raw[3]) * scale,
            "az": le_signed(raw[4], raw[5]) * scale,
        }


class _Driver(Protocol):
    kind: str
    has_gyro: bool

    def configure(self) -> None: ...
    def read(self) -> dict[str, float]: ...


#: Probed in order, so an MPU-6050 wins over an ADXL345 if both somehow answer.
DRIVERS: tuple[tuple[type[Any], int], ...] = ((_Mpu6050, MPU_ADDR), (_Adxl345, ADXL_ADDR))


class Imu:
    """Blocking I2C driver. Only the op worker thread and `ImuPoller` should call `read()`."""

    def __init__(
        self,
        bus: I2cBus | None = None,
        addrs: tuple[int, ...] = (MPU_ADDR, ADXL_ADDR),
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.bus = bus
        self.addrs = addrs
        self.clock = clock
        self.sleep = sleep
        self.gyro_bias_dps = 0.0
        # One lock, because the poller thread and the op worker thread share the bus.
        self._lock = threading.RLock()
        self._driver: _Driver | None = None
        self._probe()

    # --- detection -----------------------------------------------------------------------

    def _probe(self) -> None:
        if self.bus is None:
            return
        for cls, addr in DRIVERS:
            if addr not in self.addrs:
                continue
            if not cls.identify(self.bus, addr):
                continue
            driver = cls(self.bus, addr)
            try:
                driver.configure()
            except OSError as e:
                log.warning("%s answered but would not configure: %s", driver.kind, e)
                continue
            self._driver = driver
            return
        log.warning("no IMU at %s: turns stay timed, tilt cutoff off", [hex(a) for a in self.addrs])

    def detect(self) -> str:
        """`"mpu6050@0x68"` | `"adxl345@0x53"` | `"none"` (P2 role doc section 4)."""
        return self._driver.kind if self._driver is not None else NONE

    @property
    def kind(self) -> str:
        return self.detect()

    @property
    def present(self) -> bool:
        return self._driver is not None

    def has_gyro(self) -> bool:
        """The only switch anywhere downstream: it decides gyro-closed vs timed turns."""
        return self._driver is not None and self._driver.has_gyro

    # --- reading -------------------------------------------------------------------------

    def read(self) -> dict[str, float]:
        """One sample. `{}` when there is no IMU; raises OSError when the bus misbehaves."""
        with self._lock:
            if self._driver is None:
                return {}
            sample = self._driver.read()
        if "gz_dps" in sample:
            sample["gz_dps"] -= self.gyro_bias_dps
        return sample

    def close(self) -> None:
        with self._lock:
            self._driver = None
            if self.bus is not None:
                with_close = getattr(self.bus, "close", None)
                if callable(with_close):
                    with_close()
                self.bus = None
