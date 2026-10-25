"""Tests for rover/rover/config.py and the shipped rover/calib/calib.yaml.

The pinout assertions are deliberate regression tests: docs/01 section 4 is P1's, and if the
as-built wiring changes without doc 01 changing with it, this file goes red rather than the
rover silently driving the wrong pins. Same spirit as
gx10/brain/tests/test_settings_match_doc00.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402

from marsrover_shared.version import CONTRACT_VERSION  # noqa: E402
from rover.config import (  # noqa: E402
    CALIB_YAML,
    Calib,
    CalibError,
    interp,
    load_calib,
    parse_calib,
)

# The as-built harness, BCM numbering: an L293D, and the HC-SR04. The scoop was scrapped in
# Pi. docs/01 section 4 still describes the TB6612 + PCA9685 stack it replaced, and a request
# is open to P1 (docs/requests/2026-10-24-P2-to-P1-harness-imu-and-pinout.md). Once that
# lands, doc 01 wins over this table again -- until then this *is* the record, and comparing
# calib.yaml against it is what stops the code and the harness drifting apart silently.
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
DOC01_I2C = {"ads1115_addr": 0x48}


@pytest.fixture
def calib() -> Calib:
    """The real shipped calib.yaml, read off disk."""
    return load_calib(CALIB_YAML)


# --- the shipped file --------------------------------------------------------------------


def test_the_shipped_calib_yaml_loads():
    c = load_calib(CALIB_YAML)
    assert c.source == CALIB_YAML
    assert not c.calibrated, "calibrated_at must stay null until calibrate_*.py has run"


def test_pins_match_the_as_built_harness(calib: Calib):
    actual = {name: getattr(calib.pins, name) for name in DOC01_PINS}
    assert actual == DOC01_PINS, "calib.yaml pins drifted from the as-built harness"
    # The L293D has no standby pin; its enables are the hardware-off (see motors.stop()).
    assert not hasattr(calib.pins, "stby")
    assert calib.i2c.ads1115_addr == DOC01_I2C["ads1115_addr"]
    # docs/05 U-2: MPU-6050 first, accel-only ADXL345 as the fallback.
    assert calib.i2c.imu_addrs == (0x68, 0x53)


def test_contract_version_matches_shared(calib: Calib):
    assert calib.contract_version == CONTRACT_VERSION


def test_every_role_doc_section_4_field_is_present_and_typed(calib: Calib):
    assert isinstance(calib.motor_trim, float)
    assert calib.m_per_s_at and all(
        isinstance(k, float) and isinstance(v, float) for k, v in calib.m_per_s_at.items()
    )
    assert calib.deg_per_s_at and all(
        isinstance(k, float) and isinstance(v, float) for k, v in calib.deg_per_s_at.items()
    )
    assert len(calib.homography) == 3 and all(len(row) == 3 for row in calib.homography)
    assert calib.bev_res_m_px == 0.005  # docs/00 section 4
    assert calib.bev_size_px == 360


def test_safety_and_bev_constants_mirror_doc00(calib: Calib):
    assert calib.safety.heartbeat_timeout_s == 1.0  # docs/00 section 4
    assert calib.safety.tilt_limit_deg == 35.0  # P2 role doc section 4
    assert calib.camera.flush_frames == 3  # P2 role doc section 9


def test_the_frozen_tables_cannot_be_mutated(calib: Calib):
    with pytest.raises(TypeError):
        calib.m_per_s_at[0.4] = 0.99  # type: ignore[index]


# --- loading behaviour -------------------------------------------------------------------


def _write(tmp_path: Path, raw: dict, name: str = "calib.yaml") -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_load_is_cached_per_path(tmp_path: Path):
    path = _write(tmp_path, helpers.CALIB_DICT)
    assert load_calib(path) is load_calib(path)


def test_non_ascii_comments_are_read_as_utf8(tmp_path: Path):
    """Windows defaults to cp1252; the shipped file has degree signs in its comments."""
    path = tmp_path / "utf8.yaml"
    body = yaml.safe_dump(helpers.CALIB_DICT)
    path.write_text(
        f"# tilt cutoff is 35°, heading wraps to (-180°, 180°]\n{body}", encoding="utf-8"
    )
    assert load_calib(path).bev_size_px == 360


def test_env_var_overrides_the_default_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = _write(tmp_path, helpers.calib_dict(bev={"bev_size_px": 123}))
    monkeypatch.setenv("ROVER_CALIB", str(path))
    assert load_calib().bev_size_px == 123


def test_a_missing_file_raises_calib_error(tmp_path: Path):
    with pytest.raises(CalibError, match="cannot read"):
        load_calib(tmp_path / "nope.yaml")


def test_invalid_yaml_raises_calib_error(tmp_path: Path):
    path = tmp_path / "bad.yaml"
    path.write_text("motion: [unclosed\n", encoding="utf-8")
    with pytest.raises(CalibError, match="not valid YAML"):
        load_calib(path)


@pytest.mark.parametrize("section", ["pins", "i2c", "motion", "bev", "camera", "safety", "pose"])
def test_a_missing_section_names_itself(section: str):
    raw = {k: v for k, v in helpers.CALIB_DICT.items() if k != section}
    with pytest.raises(CalibError, match=section):
        parse_calib(raw)


def test_a_missing_key_names_itself():
    raw = helpers.calib_dict()
    raw["pins"] = {k: v for k, v in raw["pins"].items() if k != "en1"}
    with pytest.raises(CalibError, match="en1"):
        parse_calib(raw)


@pytest.mark.parametrize(
    "patch, match",
    [
        ({"bev": {"homography": [[1, 0, 0], [0, 1, 0]]}}, "homography"),
        ({"motion": {"m_per_s_at": {}}}, "m_per_s_at"),
        ({"i2c": {"imu_addrs": []}}, "imu_addrs"),
    ],
)
def test_malformed_values_are_rejected(patch: dict, match: str):
    with pytest.raises(CalibError, match=match):
        parse_calib(helpers.calib_dict(**patch))


def test_speed_table_keys_are_coerced_from_strings():
    """PyYAML can hand back string keys depending on quoting; don't trust it."""
    raw = helpers.calib_dict(motion={"m_per_s_at": {"0.3": "0.12", "1.0": "0.40"}})
    table = parse_calib(raw).m_per_s_at
    assert table == {0.3: 0.12, 1.0: 0.40}


# --- interp ------------------------------------------------------------------------------

TABLE = {0.3: 0.12, 0.5: 0.20, 0.7: 0.28, 1.0: 0.40}


@pytest.mark.parametrize(
    "speed, expected",
    [
        (0.3, 0.12),  # on a point
        (0.5, 0.20),
        (1.0, 0.40),
        (0.4, 0.16),  # halfway between 0.3 and 0.5
        (0.6, 0.24),
        (0.85, 0.34),  # halfway between 0.7 and 1.0
        (0.0, 0.12),  # clamped below
        (0.1, 0.12),
        (1.0, 0.40),  # clamped above
        (2.0, 0.40),
    ],
)
def test_interp_interpolates_and_clamps(speed: float, expected: float):
    assert interp(TABLE, speed) == pytest.approx(expected)


def test_interp_rejects_an_empty_table():
    with pytest.raises(CalibError, match="empty"):
        interp({}, 0.5)


def test_interp_handles_a_single_point_table():
    assert interp({0.5: 0.2}, 0.1) == 0.2
    assert interp({0.5: 0.2}, 0.9) == 0.2
