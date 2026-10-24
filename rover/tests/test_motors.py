"""Tests for rover/rover/motors.py -- the L293D truth table and the safety latch.

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
    motor_pins,
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
    "value, in_a_high, in_b_high",
    [(0.5, True, False), (-0.5, False, True), (0.0, False, False)],
    ids=["forward", "reverse", "stopped"],
)
def test_the_l293d_truth_table(value: float, in_a_high: bool, in_b_high: bool):
    """With the enable high: H/L forward, L/H reverse. The enable carries the PWM."""
    motors, pins = build()
    motors.set(value, value)
    assert pins.last("out", PINS.in1) is in_a_high
    assert pins.last("out", PINS.in2) is in_b_high
    assert pins.last("pwm", PINS.en1) == pytest.approx(abs(value))


def test_direction_is_set_before_the_enable_rises():
    """Otherwise a bridge spends a PWM cycle enabled with an undefined direction."""
    motors, pins = build()
    motors.set(0.8, 0.8)
    order = [(kind, pin) for kind, pin, _ in pins.writes]
    assert order.index(("out", PINS.in1)) < order.index(("pwm", PINS.en1))
    assert order.index(("out", PINS.in3)) < order.index(("pwm", PINS.en2))


def test_speeds_clamp_to_plus_minus_one():
    motors, pins = build()
    motors.set(4.0, -4.0)
    assert pins.last("pwm", PINS.en1) == pytest.approx(1.0)
    assert pins.last("pwm", PINS.en2) == pytest.approx(1.0)
    assert motors.last == (1.0, -1.0)


def test_positive_trim_slows_the_left_wheel():
    motors, pins = build(trim=0.1)
    motors.set(1.0, 1.0)
    assert pins.last("pwm", PINS.en1) == pytest.approx(0.9)
    assert pins.last("pwm", PINS.en2) == pytest.approx(1.0)


def test_negative_trim_slows_the_right_wheel():
    motors, pins = build(trim=-0.2)
    motors.set(1.0, 1.0)
    assert pins.last("pwm", PINS.en1) == pytest.approx(1.0)
    assert pins.last("pwm", PINS.en2) == pytest.approx(0.8)


def test_inversion_flips_a_backwards_motor():
    """docs/01 section 4 prefers fixing the wiring; this is the escape hatch."""
    motors, pins = build(invert_left=True)
    motors.set(0.6, 0.6)
    assert pins.last("out", PINS.in1) is False and pins.last("out", PINS.in2) is True
    assert pins.last("out", PINS.in3) is True and pins.last("out", PINS.in4) is False


# --- stop ----------------------------------------------------------------------------------


def test_stop_drops_both_enables_so_the_outputs_go_high_impedance():
    """EN low is the L293D equivalent of STBY: a hardware disable, not a 0% duty."""
    motors, pins = build()
    motors.set(1.0, 1.0)
    pins.clear()
    motors.stop()
    assert pins.last("pwm", PINS.en1) == 0.0
    assert pins.last("pwm", PINS.en2) == 0.0
    # The direction pins are left defined and de-energised.
    for pin in (PINS.in1, PINS.in2, PINS.in3, PINS.in4):
        assert pins.last("out", pin) is False
    assert motors.last == (0.0, 0.0)


def test_brake_is_opt_in_and_shorts_the_motor():
    """Equal inputs with the enables high. Faster, but it dumps the motor energy through
    the L293D when its 600 mA/channel can least take it -- so stop() coasts instead."""
    motors, pins = build()
    motors.set(1.0, 1.0)
    pins.clear()
    motors.brake()
    for pin in (PINS.in1, PINS.in2, PINS.in3, PINS.in4):
        assert pins.last("out", pin) is True
    assert pins.last("pwm", PINS.en1) == 1.0
    assert motors.last == (0.0, 0.0)


def test_stop_works_while_inhibited():
    """A latch must never be able to block a stop."""
    motors, pins = build()
    motors.inhibit("estop")
    pins.clear()
    motors.stop()
    assert pins.last("pwm", PINS.en1) == 0.0


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
    assert pins.last("pwm", PINS.en1) == pytest.approx(0.4)


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
    assert pins.last("pwm", PINS.en1) == pytest.approx(1.0)


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
    pwm, out = motor_pins(PINS)
    with pytest.raises(MotorsUnavailable, match="gpiozero"):
        GpioZeroPins(pwm, out)


def test_motor_pins_puts_pwm_only_on_the_enables():
    """The enables carry PWM; the direction pins and the LED are plain outputs."""
    pwm, out = motor_pins(PINS)
    assert pwm == {PINS.en1: PINS.pwm_hz, PINS.en2: PINS.pwm_hz}
    assert set(out) == {PINS.in1, PINS.in2, PINS.in3, PINS.in4, PINS.led}
    assert not set(pwm) & set(out), "a pin cannot be both PWM and a digital output"


def test_close_stops_and_releases_the_backend():
    motors, pins = build()
    motors.set(1.0, 1.0)
    motors.close()
    assert pins.last("pwm", PINS.en1) == 0.0
    assert pins.closed is True
