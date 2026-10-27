"""Tests for rover/rover/pid.py."""

from __future__ import annotations

import pytest

from rover.pid import Pid


def test_proportional_only():
    pid = Pid(kp=2.0)
    assert pid.update(3.0, 0.02) == pytest.approx(6.0)
    assert pid.update(-1.0, 0.02) == pytest.approx(-2.0)


def test_output_is_clamped():
    pid = Pid(kp=10.0, out_min=-0.5, out_max=0.5)
    assert pid.update(1.0, 0.02) == 0.5
    assert pid.update(-1.0, 0.02) == -0.5


def test_integral_accumulates_error_over_time():
    pid = Pid(kp=0.0, ki=1.0)
    for _ in range(10):
        out = pid.update(2.0, 0.1)
    assert out == pytest.approx(2.0)  # integral of 2.0 over 1.0 s


def test_integral_is_clamped_to_i_max():
    pid = Pid(kp=0.0, ki=1.0, i_max=0.5)
    for _ in range(100):
        out = pid.update(1.0, 0.1)
    assert out == pytest.approx(0.5)


def test_no_windup_while_the_output_is_saturated():
    """A stalled wheel must not wind the integral up and lurch when it breaks free."""
    pid = Pid(kp=1.0, ki=1.0, out_min=-1.0, out_max=1.0)
    for _ in range(200):
        pid.update(5.0, 0.1)  # saturated high the whole time
    # The error flips sign: an unwound integral responds at once instead of after 100 s.
    assert pid.update(-0.5, 0.1) < 0


def test_derivative_on_measurement_uses_the_rate():
    pid = Pid(kp=0.0, kd=0.5)
    assert pid.update(0.0, 0.02, rate=4.0) == pytest.approx(-2.0)


def test_derivative_falls_back_to_the_error_difference():
    pid = Pid(kp=0.0, kd=1.0)
    assert pid.update(1.0, 0.1) == 0.0  # no history yet
    assert pid.update(1.5, 0.1) == pytest.approx(5.0)


def test_reset_clears_the_history():
    pid = Pid(kp=0.0, ki=1.0)
    pid.update(1.0, 1.0)
    pid.reset()
    assert pid.update(0.0, 1.0) == 0.0


def test_zero_dt_is_harmless():
    pid = Pid(kp=1.0, ki=1.0, kd=1.0)
    assert pid.update(2.0, 0.0) == pytest.approx(2.0)
