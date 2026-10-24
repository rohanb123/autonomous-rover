"""Shared builders for the rover tests. Imported bare (`import helpers`), like test_protocol.py.

No `__init__.py` in rover/tests/ on purpose: `shared/py/tests` has none either, and two
packages both named `tests` on one pythonpath is a trap.

The I2C fakes return real synthetic register bytes rather than canned readings, so the tests
exercise the actual two's-complement and LSB-scaling arithmetic instead of a second fake of it.
"""

from __future__ import annotations

from typing import Any

# Re-exported (PEP 484 explicit form) so tests and `--motors sim` share one stub.
from rover.motors import RecordingPins as RecordingPins

# --- time --------------------------------------------------------------------------------


class FakeClock:
    """A monotonic clock you drive by hand. Callable, so it drops into `clock=` directly."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += dt
        return self.t

    def sleep(self, dt: float) -> None:
        """Stand-in for time.sleep that advances the clock instead of blocking."""
        self.slept.append(dt)
        self.t += dt


# --- I2C ---------------------------------------------------------------------------------


class FakeBus:
    """An I2cBus over a {addr: {reg: byte}} dict. Unknown addresses raise OSError, like real I2C."""

    def __init__(self, devices: dict[int, dict[int, int]] | None = None) -> None:
        self.devices = devices or {}
        self.writes: list[tuple[int, int, int]] = []
        self.closed = False

    def _dev(self, addr: int) -> dict[int, int]:
        if addr not in self.devices:
            raise OSError(121, "Remote I/O error")  # errno.EREMOTEIO, what smbus2 raises
        return self.devices[addr]

    def read8(self, addr: int, reg: int) -> int:
        return self._dev(addr).get(reg, 0) & 0xFF

    def read_block(self, addr: int, reg: int, length: int) -> list[int]:
        dev = self._dev(addr)
        return [dev.get(reg + i, 0) & 0xFF for i in range(length)]

    def write8(self, addr: int, reg: int, value: int) -> None:
        self._dev(addr)[reg] = value & 0xFF
        self.writes.append((addr, reg, value & 0xFF))

    def close(self) -> None:
        self.closed = True


def _be16(value: int) -> tuple[int, int]:
    """Signed 16-bit to (high, low) big-endian two's-complement bytes."""
    raw = value & 0xFFFF
    return (raw >> 8) & 0xFF, raw & 0xFF


def _le16(value: int) -> tuple[int, int]:
    """Signed 16-bit to (low, high) little-endian bytes (the ADXL345's order)."""
    raw = value & 0xFFFF
    return raw & 0xFF, (raw >> 8) & 0xFF


def fake_mpu_bus(
    ax_g: float = 0.0, ay_g: float = 0.0, az_g: float = 1.0, gz_dps: float = 0.0
) -> FakeBus:
    """An MPU-6050 at 0x68 reporting these values, encoded as real registers.

    +/-2 g -> 16384 LSB/g, +/-250 deg/s -> 131 LSB per deg/s (the power-on defaults).
    """
    regs: dict[int, int] = {0x75: 0x68}  # WHO_AM_I
    for i, g in enumerate((ax_g, ay_g, az_g)):
        hi, lo = _be16(round(g * 16384))
        regs[0x3B + 2 * i], regs[0x3C + 2 * i] = hi, lo
    hi, lo = _be16(round(gz_dps * 131))
    regs[0x47], regs[0x48] = hi, lo
    return FakeBus({0x68: regs})


def fake_adxl_bus(ax_g: float = 0.0, ay_g: float = 0.0, az_g: float = 1.0) -> FakeBus:
    """An ADXL345 at 0x53: DEVID 0xE5, 256 LSB/g full-res, little-endian, no gyro."""
    regs: dict[int, int] = {0x00: 0xE5}
    for i, g in enumerate((ax_g, ay_g, az_g)):
        lo, hi = _le16(round(g * 256))
        regs[0x32 + 2 * i], regs[0x33 + 2 * i] = lo, hi
    return FakeBus({0x53: regs})


def fake_pca_bus(addr: int = 0x40) -> FakeBus:
    return FakeBus({addr: {0x00: 0x11}})  # MODE1 with SLEEP set, as after power-on


# --- calib -------------------------------------------------------------------------------

#: A complete calib.yaml as a dict, matching rover/calib/calib.yaml's shape. Tests override
#: single keys rather than restating the whole thing.
CALIB_DICT: dict[str, Any] = {
    "contract_version": "1.3.0",
    "calibrated_at": None,
    "pins": {
        "pwm_a": 12,
        "ain1": 5,
        "ain2": 6,
        "pwm_b": 13,
        "bin1": 20,
        "bin2": 21,
        "stby": 26,
        "led": 22,
        "pwm_hz": 1000,
    },
    "i2c": {"bus": 1, "pca9685_addr": 0x40, "imu_addrs": [0x68, 0x53], "ads1115_addr": 0x48},
    "motion": {
        "invert_left": False,
        "invert_right": False,
        "min_speed": 0.15,
        "turn_settle_s": 0.3,
        "gyro_turn_gain": 1.0,
        "motor_trim": 0.0,
        "m_per_s_at": {0.3: 0.12, 0.5: 0.20, 0.7: 0.28, 1.0: 0.40},
        "deg_per_s_at": {0.3: 45.0, 0.5: 80.0, 0.7: 115.0, 1.0: 165.0},
    },
    "bev": {
        "bev_res_m_px": 0.005,
        "bev_size_px": 360,
        "homography": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
    },
    "camera": {
        "index": 0,
        "width": 640,
        "height": 480,
        "fps": 30,
        "fourcc": "MJPG",
        "flush_frames": 3,
        "settle_s": 0.3,
        "jpeg_quality": 85,
        "lock_exposure": True,
        "exposure": 80.0,
        "lock_white_balance": True,
        "exposure_us": 8000,
        "analogue_gain": 2.0,
        "focus_m": 0.8,
        "present_focus_m": 0.15,
    },
    "servos": {
        "freq_hz": 50,
        "shoulder_ch": 0,
        "wrist_ch": 1,
        "limits_us": [600, 2400],
        "ramp_s": 0.4,
        "scoop_us": {
            "STOW": [1500, 1500],
            "DEPLOY": [1900, 1200],
            "CURL": [1700, 1800],
            "PRESENT": [1300, 1750],
            "DUMP": [1850, 1100],
        },
    },
    "scoop": {
        "scoop_zone_px": [240, 330, 160, 120],
        "present_roi_px": [160, 90, 320, 240],
        "push_m": 0.04,
        "push_speed": 0.2,
    },
    "safety": {
        "tilt_limit_deg": 35.0,
        "heartbeat_timeout_s": 1.0,
        "stop_tick_s": 0.02,
        "tilt_clear_ticks": 5,
    },
    "pose": {"sigma_xy_m0": 0.01, "sigma_xy_per_m": 0.05, "sigma_xy_max_m": 0.50},
}


def calib_dict(**sections: dict[str, Any]) -> dict[str, Any]:
    """CALIB_DICT with whole sections shallow-merged, e.g. calib_dict(motion={"min_speed": 0.5})."""
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in CALIB_DICT.items()}
    for name, patch in sections.items():
        if isinstance(out.get(name), dict):
            out[name] = {**out[name], **patch}
        else:
            out[name] = patch
    return out
