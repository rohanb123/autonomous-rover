"""Tests for rover/rover/motors.py -- the TB6612 truth table and the safety latch.

The latch tests are the ones that matter. A long op runs on a worker thread calling `set()`
every 20 ms; if `stop()` doesn't latch the motors off, the worker re-enables them on its next
pass and the E-STOP never took (docs/05 R14).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402

from rover.config import parse_calib  # noqa: E402
from rover.motors import (  # noqa: E402
    DOC01_PINS,
    GpioZeroPins,
    Motors,
    MotorsUnavailable,
    NullPins,
    RecordingPins,
)

PINS = parse_calib(helpers.CALIB_DICT).pins


def build(trim: float = 0.0, **kwargs) -> tuple[Motors, RecordingPins]:
    pins = RecordingPins()
    return Motors(PINS, trim, pins, **kwargs), pins


def test_doc01_pins_constant_matches_calib():
    """The module's reference copy and the loaded calibration must not disagree."""
    assert {name: getattr(PINS, name) for name in DOC01_PINS} == DOC01_PINS


def test_present_reflects_the_backend():
    assert build()[0].present is True
    assert Motors(PINS, 0.0, NullPins()).present is False
    assert Motors(PINS, 0.0).present is False  # defaults to no hardware


# --- direction and speed -------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, in1_high, in2_high",
    [(0.5, True, False), (-0.5, False, True), (0.0, False, False)],
    ids=["forward", "reverse", "coast"],
)
def test_the_tb6612_truth_table(value: float, in1_high: bool, in2_high: bool):
    motors, pins = build()
    motors.set(value, value)
    assert pins.last("out", PINS.ain1) is in1_high
    assert pins.last("out", PINS.ain2) is in2_high
    assert pins.last("pwm", PINS.pwm_a) == pytest.approx(abs(value))
    assert pins.last("out", PINS.stby) is True, "STBY must go high to enable the driver"


def test_speeds_clamp_to_plus_minus_one():
    motors, pins = build()
    motors.set(4.0, -4.0)
    assert pins.last("pwm", PINS.pwm_a) == pytest.approx(1.0)
    assert pins.last("pwm", PINS.pwm_b) == pytest.approx(1.0)
    assert motors.last == (1.0, -1.0)


def test_positive_trim_slows_the_left_wheel():
    motors, pins = build(trim=0.1)
    motors.set(1.0, 1.0)
    assert pins.last("pwm", PINS.pwm_a) == pytest.approx(0.9)
    assert pins.last("pwm", PINS.pwm_b) == pytest.approx(1.0)


def test_negative_trim_slows_the_right_wheel():
    motors, pins = build(trim=-0.2)
    motors.set(1.0, 1.0)
    assert pins.last("pwm", PINS.pwm_a) == pytest.approx(1.0)
    assert pins.last("pwm", PINS.pwm_b) == pytest.approx(0.8)


def test_inversion_flips_a_backwards_motor():
    """docs/01 section 4 prefers fixing the wiring; this is the escape hatch."""
    motors, pins = build(invert_left=True)
    motors.set(0.6, 0.6)
    assert pins.last("out", PINS.ain1) is False and pins.last("out", PINS.ain2) is True
    assert pins.last("out", PINS.bin1) is True and pins.last("out", PINS.bin2) is False


# --- stop ----------------------------------------------------------------------------------


def test_stop_brakes_then_standbys():
    motors, pins = build()
    motors.set(1.0, 1.0)
    pins.clear()
    motors.stop()
    assert pins.last("pwm", PINS.pwm_a) == 0.0
    assert pins.last("pwm", PINS.pwm_b) == 0.0
    # IN1 = IN2 = HIGH is TB6612 short brake.
    for pin in (PINS.ain1, PINS.ain2, PINS.bin1, PINS.bin2):
        assert pins.last("out", pin) is True
    assert pins.last("out", PINS.stby) is False, "STBY LOW = motors off is the safety default"
    assert motors.last == (0.0, 0.0)
    # Ordering: PWM to zero before standby, so nothing coasts at speed.
    kinds = [f"{kind}{pin}" for kind, pin, _ in pins.writes]
    assert kinds.index(f"pwm{PINS.pwm_a}") < kinds.index(f"out{PINS.stby}")


def test_stop_works_while_inhibited():
    """A latch must never be able to block a stop."""
    motors, pins = build()
    motors.inhibit("estop")
    pins.clear()
    motors.stop()
    assert pins.last("out", PINS.stby) is False


# --- the latch -----------------------------------------------------------------------------


def test_set_is_a_noop_while_inhibited():
    motors, pins = build()
    motors.inhibit("estop")
    pins.clear()
    motors.set(1.0, 1.0)
    assert pins.writes == [], "a worker thread must not be able to undo a stop"
    assert motors.inhibited and motors.latches == {"estop"}


def test_releasing_restores_driving():
    motors, pins = build()
    motors.inhibit("estop")
    motors.set(1.0, 1.0)
    motors.release("estop")
    assert not motors.inhibited
    motors.set(0.4, 0.4)
    assert pins.last("pwm", PINS.pwm_a) == pytest.approx(0.4)


def test_two_latches_must_both_clear():
    """The watchdog clears on the next heartbeat; estop only on clear_estop."""
    motors, pins = build()
    motors.inhibit("estop")
    motors.inhibit("watchdog")
    assert motors.latches == {"estop", "watchdog"}
    motors.release("watchdog")
    assert motors.inhibited, "still held by estop"
    pins.clear()
    motors.set(1.0, 1.0)
    assert pins.writes == []
    motors.release("estop")
    motors.set(1.0, 1.0)
    assert pins.last("pwm", PINS.pwm_a) == pytest.approx(1.0)


def test_latching_and_releasing_are_idempotent():
    motors, _ = build()
    motors.inhibit("tilt")
    motors.inhibit("tilt")
    motors.release("tilt")
    assert not motors.inhibited
    motors.release("tilt")  # releasing something not held must not raise
    motors.release("never-held")


# --- backends ------------------------------------------------------------------------------


def test_gpiozero_backend_reports_unavailable_without_the_library(monkeypatch):
    monkeypatch.setitem(sys.modules, "gpiozero", None)
    with pytest.raises(MotorsUnavailable, match="gpiozero"):
        GpioZeroPins(PINS)


def test_close_stops_and_releases_the_backend():
    motors, pins = build()
    motors.set(1.0, 1.0)
    motors.close()
    assert pins.last("out", PINS.stby) is False
    assert pins.closed is True
