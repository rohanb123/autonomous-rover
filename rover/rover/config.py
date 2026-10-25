"""Calibration loader for the Pi runtime: `rover/calib/calib.yaml` -> `Calib`.

Loaded once, read-only. The nine fields P2's role doc section 4 names stay flat on `Calib`
(`motor_trim`, `m_per_s_at`, `deg_per_s_at`, `homography`, `bev_res_m_px`, `bev_size_px`,
`homography`); everything else hangs off a nested frozen
group. The YAML is nested for readability, so this module is where the two shapes meet.

`pins` is a copy of docs/01-hardware-and-assembly.md section 4 and `rover/tests/test_config.py`
compares the two: a wiring change that never reaches doc 01 fails the build on purpose.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import MISSING, dataclass, field
from functools import cache
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CALIB_YAML = ROOT / "rover" / "calib" / "calib.yaml"


class CalibError(ValueError):
    """calib.yaml is missing, unreadable, or missing a key the runtime needs."""


# --- nested groups -----------------------------------------------------------------------


@dataclass(frozen=True)
class Pins:
    """BCM pin numbers for the L293D, the status LED and the HC-SR04.

    The L293D has no STBY: its enable pins are the equivalent, and EN low puts the outputs
    in high-impedance so the motors coast. That is the safety-off state.
    """

    en1: int  # L293D pin 1  -- left PWM *and* enable
    in1: int  # L293D pin 2  -- left direction
    in2: int  # L293D pin 7  -- left direction
    en2: int  # L293D pin 9  -- right PWM and enable
    in3: int  # L293D pin 10 -- right direction
    in4: int  # L293D pin 15 -- right direction
    led: int
    pwm_hz: int
    # HC-SR04 (contract 2.0.0). Defaulted so a calib.yaml without them still loads;
    # ECHO needs a 1k/2k divider, it is a 5 V output into a 3.3 V-max pin.
    sonar_trig: int = 23
    sonar_echo: int = 24


@dataclass(frozen=True)
class I2c:
    bus: int
    imu_addrs: tuple[int, ...]
    ads1115_addr: int


@dataclass(frozen=True)
class MotionCfg:
    invert_left: bool
    invert_right: bool
    min_speed: float
    turn_settle_s: float
    gyro_turn_gain: float


@dataclass(frozen=True)
class CameraCfg:
    index: int
    width: int
    height: int
    fps: int
    fourcc: str  # USB path only; the CSI path ignores it
    flush_frames: int
    settle_s: float
    jpeg_quality: int
    lock_exposure: bool
    exposure: float  # USB/V4L2 driver units
    lock_white_balance: bool
    # --- CSI (Camera Module 3) path. Defaulted, so an older calib.yaml still loads.
    exposure_us: int = 8000  # libcamera ExposureTime is microseconds, not driver units
    analogue_gain: float = 2.0  # with AeEnable off, gain has to be set explicitly
    focus_m: float = 0.8  # survey: ground 0.3-1.5 m out
    present_focus_m: float = 0.15  # the close-up `sample` photo (docs/05 U-7)
    lores_width: int = 320  # second CSI stream, for :8080. Height follows main's aspect
    rotate_180: bool = False  # camera mounted upside down: every frame is turned 180 deg


@dataclass(frozen=True)
class SafetyCfg:
    tilt_limit_deg: float
    heartbeat_timeout_s: float
    stop_tick_s: float
    tilt_clear_ticks: int


@dataclass(frozen=True)
class PoseCfg:
    sigma_xy_m0: float
    sigma_xy_per_m: float
    sigma_xy_max_m: float


@dataclass(frozen=True)
class Calib:
    """The nine role-doc section 4 fields, flat, plus the nested groups."""

    # role doc section 4, verbatim
    motor_trim: float
    m_per_s_at: Mapping[float, float]
    deg_per_s_at: Mapping[float, float]
    homography: tuple[tuple[float, float, float], ...]
    bev_res_m_px: float
    bev_size_px: int
    # the rest
    pins: Pins
    i2c: I2c
    motion: MotionCfg
    camera: CameraCfg
    safety: SafetyCfg
    pose: PoseCfg
    contract_version: str
    calibrated_at: str | None = None
    source: Path | None = field(default=None, compare=False)

    @property
    def calibrated(self) -> bool:
        """False while the numbers are the shipped placeholders, not measurements."""
        return self.calibrated_at is not None


# --- loading -----------------------------------------------------------------------------


def _group(raw: dict[str, Any], name: str) -> dict[str, Any]:
    section = raw.get(name)
    if not isinstance(section, dict):
        raise CalibError(f"calib.yaml: missing or malformed {name!r} section")
    return section


def _build(cls: type, section: dict[str, Any], name: str, **coerced: Any):
    """Construct a frozen group, naming the exact missing key instead of a bare TypeError.

    `coerced` wins over the raw YAML for the same key, so a caller that pre-parsed a value
    (a tuple out of a list, say) doesn't pass it twice.
    """
    fields = set(cls.__dataclass_fields__)
    # Only fields without a default are mandatory, so adding a defaulted one (a new CSI
    # camera control, say) doesn't break a calib.yaml written before it existed.
    required = {
        f
        for f, spec in cls.__dataclass_fields__.items()
        if spec.default is MISSING and spec.default_factory is MISSING
    }
    missing = required - set(section) - set(coerced)
    if missing:
        raise CalibError(f"calib.yaml: {name} is missing {sorted(missing)}")
    raw = {k: v for k, v in section.items() if k in fields and k not in coerced}
    return cls(**raw, **coerced)


def _speed_table(section: dict[str, Any], key: str) -> Mapping[float, float]:
    """PyYAML's key typing for `0.3:` is not something to rely on, so coerce both sides."""
    table = section.get(key)
    if not isinstance(table, dict) or not table:
        raise CalibError(f"calib.yaml: motion.{key} must be a non-empty mapping")
    return MappingProxyType({float(k): float(v) for k, v in table.items()})


def _quad(section: dict[str, Any], key: str, name: str) -> tuple[int, int, int, int]:
    box = section.get(key)
    if not isinstance(box, list) or len(box) != 4:
        raise CalibError(f"calib.yaml: {name}.{key} must be a list of 4 ints [x, y, w, h]")
    return (int(box[0]), int(box[1]), int(box[2]), int(box[3]))


def parse_calib(raw: dict[str, Any], source: Path | None = None) -> Calib:
    """Build a `Calib` from already-parsed YAML. Separate from IO so tests can feed dicts."""
    if not isinstance(raw, dict):
        raise CalibError("calib.yaml: top level must be a mapping")
    motion, bev, i2c = _group(raw, "motion"), _group(raw, "bev"), _group(raw, "i2c")

    homography = bev.get("homography")
    if not isinstance(homography, list) or len(homography) != 3:
        raise CalibError("calib.yaml: bev.homography must be a 3x3 list of lists")
    for row in homography:
        if not isinstance(row, list) or len(row) != 3:
            raise CalibError("calib.yaml: bev.homography must be a 3x3 list of lists")

    imu_addrs = i2c.get("imu_addrs")
    if not isinstance(imu_addrs, list) or not imu_addrs:
        raise CalibError("calib.yaml: i2c.imu_addrs must be a non-empty list")

    version = raw.get("contract_version")
    if not isinstance(version, str):
        raise CalibError("calib.yaml: contract_version must be a string")

    return Calib(
        motor_trim=float(motion.get("motor_trim", 0.0)),
        m_per_s_at=_speed_table(motion, "m_per_s_at"),
        deg_per_s_at=_speed_table(motion, "deg_per_s_at"),
        homography=tuple(tuple(float(v) for v in row) for row in homography),  # type: ignore[arg-type]
        bev_res_m_px=float(bev["bev_res_m_px"]),
        bev_size_px=int(bev["bev_size_px"]),
        pins=_build(Pins, _group(raw, "pins"), "pins"),
        i2c=_build(I2c, i2c, "i2c", imu_addrs=tuple(int(a) for a in imu_addrs)),
        motion=_build(MotionCfg, motion, "motion"),
        camera=_build(CameraCfg, _group(raw, "camera"), "camera"),
        safety=_build(SafetyCfg, _group(raw, "safety"), "safety"),
        pose=_build(PoseCfg, _group(raw, "pose"), "pose"),
        contract_version=version,
        calibrated_at=raw.get("calibrated_at"),
        source=source,
    )


@cache
def load_calib(path: str | Path | None = None) -> Calib:
    """Read calib.yaml. Cached; the path is injectable so tests never touch the real file.

    Resolution order: the argument, then $ROVER_CALIB, then rover/calib/calib.yaml.
    """
    target = Path(path) if path is not None else Path(os.environ.get("ROVER_CALIB", CALIB_YAML))
    try:
        # encoding is explicit on purpose: Windows defaults to cp1252 and chokes on the
        # degree signs in the comments (the same trap P4 already fixed once repo-wide).
        text = target.read_text(encoding="utf-8")
    except OSError as e:
        raise CalibError(f"cannot read {target}: {e}") from e
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise CalibError(f"{target} is not valid YAML: {e}") from e
    return parse_calib(raw, source=target)


def interp(table: Mapping[float, float], speed: float) -> float:
    """Linearly interpolate a calibration table, clamping outside its end points.

    The speed tables are sparse measurements (0.3/0.5/0.7/1.0), so a `drive` at 0.6 has to
    land between two of them. Clamping beats extrapolating: past the last measurement we
    have no evidence, and guessing high there means driving further than asked.
    """
    if not table:
        raise CalibError("empty calibration table")
    keys = sorted(table)
    if speed <= keys[0]:
        return float(table[keys[0]])
    if speed >= keys[-1]:
        return float(table[keys[-1]])
    for lo, hi in zip(keys, keys[1:], strict=False):  # keys[1:] is one shorter by design
        if lo <= speed <= hi:
            span = hi - lo
            if span <= 0:  # duplicate keys after float coercion
                return float(table[lo])
            frac = (speed - lo) / span
            return float(table[lo]) + frac * (float(table[hi]) - float(table[lo]))
    return float(table[keys[-1]])  # unreachable; keeps the type checker and mypy honest
