"""rover/calib/calibrate_motion.py and rover/calib/calib_edit.py.

The driving is not tested (it needs motors); every number it derives and every byte it
writes is. These are the values `Motion.drive` divides by, so an error here is a rover that
confidently drives into a wall.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "shared" / "py")]

from rover.config import parse_calib  # noqa: E402


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "rover" / "calib" / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cm = _load("calibrate_motion")
ce = _load("calib_edit")

CALIB_TEXT = (_ROOT / "rover" / "calib" / "calib.yaml").read_text(encoding="utf-8")


# --- speed and turn rate ------------------------------------------------------------------


def test_speed_from_run():
    assert cm.speed_from_run(0.40, 2.0) == pytest.approx(0.20)


def test_rate_from_turn_uses_the_magnitude():
    """A clockwise spin is the same rate as a counter-clockwise one."""
    assert cm.rate_from_turn(180.0, 2.0) == pytest.approx(90.0)
    assert cm.rate_from_turn(-180.0, 2.0) == pytest.approx(90.0)


@pytest.mark.parametrize(
    "dist,secs,needle",
    [
        (0.0, 2.0, "positive"),
        (-0.4, 2.0, "invert_left"),  # points at the real fix, not at negating the input
        (0.4, 0.0, "positive"),
        (40.0, 2.0, "plausible"),  # centimetres typed as metres
    ],
)
def test_speed_from_run_rejects_nonsense(dist, secs, needle):
    with pytest.raises(cm.CalibrationError, match=needle):
        cm.speed_from_run(dist, secs)


def test_rate_from_turn_rejects_an_implausible_rate():
    with pytest.raises(cm.CalibrationError, match="plausible"):
        cm.rate_from_turn(36000.0, 1.0)


# --- trim ---------------------------------------------------------------------------------


def test_trim_is_zero_for_a_straight_drive():
    assert cm.trim_from_drift(0.0, 1.0, 0.13) == pytest.approx(0.0)


def test_drifting_left_slows_the_right_wheel():
    """+drift is to the rover's LEFT, so the right wheel is fast. Motors._trimmed reads a
    NEGATIVE trim as "slow the right", so the sign must come out negative."""
    trim = cm.trim_from_drift(0.05, 1.0, 0.13)
    assert trim < 0, "a left drift must slow the right wheel"
    assert trim == pytest.approx(-2 * 0.05 * 0.13 / 1.0)


def test_drifting_right_slows_the_left_wheel():
    assert cm.trim_from_drift(-0.05, 1.0, 0.13) > 0


def test_trim_matches_what_motors_does_with_it():
    """The sign convention, checked against the real implementation rather than restated."""
    from rover.motors import Motors, RecordingPins

    calib = parse_calib(yaml.safe_load(CALIB_TEXT))
    trim = cm.trim_from_drift(0.05, 1.0, 0.13)  # drifted left
    motors = Motors(calib.pins, trim, RecordingPins())
    left, right = motors._trimmed(0.5, 0.5)
    assert right < left, "the fast (right) wheel must end up slower than the left"


def test_an_absurd_drift_is_rejected_rather_than_written():
    with pytest.raises(cm.CalibrationError, match="too large"):
        cm.trim_from_drift(2.0, 1.0, 0.13)


# --- partial runs -------------------------------------------------------------------------


def test_merge_table_keeps_unmeasured_keys():
    """A run that measured only two duties must not silently delete the other two."""
    existing = {0.3: 0.12, 0.5: 0.20, 0.7: 0.28, 1.0: 0.40}
    merged = cm.merge_table(existing, {0.3: 0.15, 0.5: 0.25})
    assert merged == {0.3: 0.15, 0.5: 0.25, 0.7: 0.28, 1.0: 0.40}


# --- writing calib.yaml -------------------------------------------------------------------


def test_replace_table_writes_a_parseable_table():
    out = ce.replace_table(CALIB_TEXT, "m_per_s_at", {0.3: 0.111, 0.5: 0.222}, "measured")
    assert yaml.safe_load(out)["motion"]["m_per_s_at"] == {0.3: 0.111, 0.5: 0.222}


def test_replace_table_leaves_no_rows_of_the_old_table_behind():
    """Half a replaced table parses fine and is wrong, which is the dangerous failure."""
    out = ce.replace_table(CALIB_TEXT, "m_per_s_at", {0.3: 0.111}, "measured")
    table = yaml.safe_load(out)["motion"]["m_per_s_at"]
    assert table == {0.3: 0.111}
    assert "0.28" not in out.split("deg_per_s_at")[0].split("m_per_s_at")[1]


def test_replace_table_does_not_touch_its_neighbour():
    out = ce.replace_table(CALIB_TEXT, "m_per_s_at", {0.3: 0.111}, "measured")
    assert yaml.safe_load(out)["motion"]["deg_per_s_at"] == {
        0.3: 45.0,
        0.5: 80.0,
        0.7: 115.0,
        1.0: 165.0,
    }


def test_replace_scalar_keeps_the_rest_of_the_file():
    out = ce.replace_scalar(CALIB_TEXT, "motor_trim", -0.0234, "measured")
    assert yaml.safe_load(out)["motion"]["motor_trim"] == pytest.approx(-0.0234)
    assert "READ THIS BEFORE TRUSTING A NUMBER" in out
    assert "L293D pin 1" in out


def test_the_written_file_still_loads_as_a_calib():
    """The real gate: whatever is written must survive parse_calib, not just yaml.safe_load."""
    out = ce.replace_table(CALIB_TEXT, "m_per_s_at", {0.3: 0.111, 1.0: 0.444}, "measured")
    out = ce.replace_table(out, "deg_per_s_at", {0.3: 40.0, 1.0: 150.0}, "measured")
    out = ce.replace_scalar(out, "motor_trim", 0.02, "measured")
    calib = parse_calib(yaml.safe_load(out))
    assert calib.m_per_s_at == {0.3: 0.111, 1.0: 0.444}
    assert calib.motor_trim == pytest.approx(0.02)


def test_a_one_point_oh_key_survives_as_a_number():
    """fmt_num writes 1.0 as `1`; config._speed_table coerces float(k), so interp still
    works. Pinned because a string key would break interp silently."""
    out = ce.replace_table(CALIB_TEXT, "m_per_s_at", {1.0: 0.4}, "measured")
    calib = parse_calib(yaml.safe_load(out))
    assert calib.m_per_s_at == {1.0: 0.4}


def test_calibrate_motion_does_not_stamp_calibrated_at():
    """calibrated_at is what Station.geometry_ok reads to allow uploading a BEV and proposal
    positions. Only the homography makes those meaningful, so only calibrate_camera.py may
    set it -- otherwise a motion-only run would license a station to upload fiction."""
    source = (_ROOT / "rover" / "calib" / "calibrate_motion.py").read_text(encoding="utf-8")
    assert "set_calibrated_at" not in source


def test_missing_keys_raise_rather_than_silently_doing_nothing():
    for call in (
        lambda: ce.replace_scalar("a: 1\n", "nope", 1.0),
        lambda: ce.replace_table("a: 1\n", "nope", {0.3: 1.0}),
        lambda: ce.set_calibrated_at("a: 1\n", "2026-10-25T00:00:00+00:00"),
    ):
        with pytest.raises(ce.CalibEditError):
            call()


def test_refuses_to_write_an_empty_table():
    with pytest.raises(ce.CalibEditError, match="empty"):
        ce.replace_table(CALIB_TEXT, "m_per_s_at", {})


# --- the duties must stay inside the usable band ------------------------------------------


def test_the_measured_duties_are_all_at_or_above_min_speed():
    """interp clamps below its first key and a station turns at exactly min_speed, so a duty
    below min_speed would be timed from a measurement that can never be requested."""
    calib = parse_calib(yaml.safe_load(CALIB_TEXT))
    assert min(cm.DUTIES) >= calib.motion.min_speed
