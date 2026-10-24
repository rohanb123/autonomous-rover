"""Tests for rover/rover/servos.py -- GPIO servos (default) and the PCA9685 fallback.

docs/05 U-23 is "PCA9685 Blinka library or plain smbus2?". The answer is smbus2, and the
reason this file exists is that it pins down the two pieces of arithmetic that could actually
be wrong -- `prescale_for` and `us_to_ticks` -- against the datasheet, on a laptop. That
leaves U-23 risking only the I2C transport, which is the part a unit test could never settle.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402

from rover.config import parse_calib  # noqa: E402
from rover.motors import RecordingPins  # noqa: E402
from rover.servos import (  # noqa: E402
    FULL_OFF,
    LED0_ON_L,
    MODE1,
    MODE1_SLEEP,
    PRESCALE,
    GpioServos,
    Pca9685Servos,
    ServosUnavailable,
    Smbus2Bus,
    duty_for_us,
    no_servos,
    prescale_for,
    servo_pins,
    us_to_ticks,
)

ADDR = 0x40


def build(**kwargs) -> tuple[Pca9685Servos, helpers.FakeBus]:
    bus = helpers.fake_pca_bus(ADDR)
    return Pca9685Servos(ADDR, 50, bus, sleep=lambda _s: None, **kwargs), bus


# --- the arithmetic ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "freq, expected",
    [
        (50, 121),  # the servo case: 25 MHz / (4096 * 50) - 1 = 121
        (60, 101),  # 25e6 / (4096 * 60) = 101.725 -> 102 - 1
        (1000, 5),
        (200, 30),
    ],
)
def test_prescale_for_matches_the_datasheet(freq: int, expected: int):
    assert prescale_for(freq) == expected


def test_prescale_clamps_to_the_legal_range():
    assert prescale_for(10) == 255  # would be 609; the register is 8 bits
    assert prescale_for(100_000) == 3  # datasheet minimum
    with pytest.raises(ValueError, match="positive"):
        prescale_for(0)


@pytest.mark.parametrize(
    "us, expected",
    [
        (1500, 307),  # centre at 50 Hz: 1500 * 4096 * 50 / 1e6
        (600, 123),
        (2400, 492),
        (0, 0),
        (20_000, 4095),  # a full period, clamped to the last tick
    ],
)
def test_us_to_ticks(us: int, expected: int):
    assert us_to_ticks(us, 50) == expected


def test_us_to_ticks_scales_with_frequency():
    assert us_to_ticks(1500, 100) == 2 * us_to_ticks(1500, 50)


# --- configuration -------------------------------------------------------------------------


def test_configure_sleeps_the_chip_to_set_the_prescaler():
    _, bus = build()
    writes = [(reg, value) for _addr, reg, value in bus.writes]
    regs = [reg for reg, _ in writes]
    assert PRESCALE in regs
    prescale_at = regs.index(PRESCALE)
    # The datasheet requires SLEEP before PRESCALE is writable.
    assert any(reg == MODE1 and value & MODE1_SLEEP for reg, value in writes[:prescale_at])
    assert dict(writes)[PRESCALE] == prescale_for(50)
    # And it must be awake afterwards, or nothing moves.
    after = [value for reg, value in writes[prescale_at + 1 :] if reg == MODE1]
    assert after and not (after[-1] & MODE1_SLEEP)


def test_no_chip_on_the_bus_is_reported_not_swallowed():
    empty = helpers.FakeBus({})  # nothing answers at 0x40
    with pytest.raises(ServosUnavailable, match="0x40"):
        Pca9685Servos(ADDR, 50, empty, sleep=lambda _s: None)


def test_smbus2_backend_reports_unavailable_without_the_library(monkeypatch):
    monkeypatch.setitem(sys.modules, "smbus2", None)
    with pytest.raises(ServosUnavailable, match="smbus2"):
        Smbus2Bus(1)


def test_present_is_false_without_a_bus():
    servos = Pca9685Servos(ADDR, 50, None)
    assert servos.present is False
    servos.write_us(0, 1500)  # must not raise: main.py offers --servos off
    assert servos.last_us(0) is None


# --- write_us ------------------------------------------------------------------------------


def test_write_us_emits_the_four_channel_registers_in_order():
    servos, bus = build()
    bus.writes.clear()
    servos.write_us(1, 1500)
    base = LED0_ON_L + 4 * 1
    ticks = us_to_ticks(1500, 50)
    assert [(reg, value) for _a, reg, value in bus.writes] == [
        (base + 0, 0x00),  # ON_L
        (base + 1, 0x00),  # ON_H
        (base + 2, ticks & 0xFF),  # OFF_L
        (base + 3, (ticks >> 8) & 0x0F),  # OFF_H
    ]
    assert servos.last_us(1) == 1500


@pytest.mark.parametrize("us, expected", [(100, 600), (3000, 2400), (1200, 1200)])
def test_write_us_clamps_to_the_calibrated_limits(us: int, expected: int):
    """docs/05 U-5: the limits are a guess until P1 reports the real stops, so clamp hard."""
    servos, _ = build(limits_us=(600, 2400))
    servos.write_us(0, us)
    assert servos.last_us(0) == expected


def test_off_sets_the_full_off_bit():
    servos, bus = build()
    servos.write_us(0, 1500)
    bus.writes.clear()
    servos.off(0)
    assert [(reg, value) for _a, reg, value in bus.writes] == [(LED0_ON_L + 3, FULL_OFF)]
    assert servos.last_us(0) is None


@pytest.mark.parametrize("ch", [-1, 16, 99])
def test_an_out_of_range_channel_is_rejected(ch: int):
    servos, _ = build()
    with pytest.raises(ValueError, match="channel"):
        servos.write_us(ch, 1500)
    with pytest.raises(ValueError, match="channel"):
        servos.off(ch)


def test_close_releases_every_held_channel():
    """A stalled servo that is still being driven cooks."""
    servos, bus = build()
    servos.write_us(0, 1500)
    servos.write_us(1, 1800)
    bus.writes.clear()
    servos.close()
    released = {reg for _a, reg, value in bus.writes if value == FULL_OFF}
    assert released == {LED0_ON_L + 3, LED0_ON_L + 7}
    assert bus.closed is True
    assert servos.present is False


# --- GpioServos: the real driver now ------------------------------------------------------

SERVO_PINS = {0: 18, 1: 19}


def gpio(**kwargs) -> tuple[GpioServos, RecordingPins]:
    pins = RecordingPins()
    return GpioServos(SERVO_PINS, 50, pins, limits_us=(600, 2400), **kwargs), pins


@pytest.mark.parametrize(
    "us, freq, duty",
    [
        (1500, 50, 0.075),  # centre, 1.5 ms of a 20 ms frame
        (600, 50, 0.030),
        (2400, 50, 0.120),
        (1500, 100, 0.150),  # halve the frame, double the duty
        (0, 50, 0.0),
    ],
)
def test_duty_for_us(us: float, freq: float, duty: float):
    assert duty_for_us(us, freq) == pytest.approx(duty)


def test_duty_is_clamped_and_rejects_a_bad_frequency():
    assert duty_for_us(99_999, 50) == 1.0  # longer than the frame
    assert duty_for_us(-100, 50) == 0.0
    with pytest.raises(ValueError, match="positive"):
        duty_for_us(1500, 0)


def test_servo_pins_keeps_channel_zero_meaning_shoulder():
    """docs/01 section 4 numbers the channels; the GPIO backend just maps them to pins."""
    calib = parse_calib(helpers.CALIB_DICT)
    assert servo_pins(calib.pins, calib.servos) == {
        calib.servos.shoulder_ch: calib.pins.servo_shoulder,
        calib.servos.wrist_ch: calib.pins.servo_wrist,
    }


def test_write_us_becomes_a_pwm_duty_on_the_mapped_pin():
    servos, pins = gpio()
    servos.write_us(0, 1500)
    assert pins.last("pwm", 18) == pytest.approx(0.075)
    assert pins.last("pwm", 19) is None, "the wrist must not move when the shoulder does"
    servos.write_us(1, 1900)
    assert pins.last("pwm", 19) == pytest.approx(duty_for_us(1900, 50))
    assert servos.last_us(0) == 1500 and servos.last_us(1) == 1900


@pytest.mark.parametrize("us, expected", [(100, 600), (3000, 2400), (1200, 1200)])
def test_gpio_write_us_clamps_to_the_calibrated_limits(us: int, expected: int):
    """docs/05 U-5: the limits are a guess until P1 reports the stops, so clamp hard."""
    servos, pins = gpio()
    servos.write_us(0, us)
    assert servos.last_us(0) == expected
    assert pins.last("pwm", 18) == pytest.approx(duty_for_us(expected, 50))


def test_off_stops_the_pulse_train_so_the_servo_goes_limp():
    """A servo held against a mechanical stall cooks itself."""
    servos, pins = gpio()
    servos.write_us(0, 1500)
    servos.off(0)
    assert pins.last("pwm", 18) == 0.0
    assert servos.last_us(0) is None


def test_an_unwired_channel_is_rejected_by_name():
    servos, _ = gpio()
    with pytest.raises(ValueError, match="channel 7 is not wired"):
        servos.write_us(7, 1500)
    with pytest.raises(ValueError, match="not wired"):
        servos.off(7)


def test_close_releases_every_channel_then_the_backend():
    servos, pins = gpio()
    servos.write_us(0, 1500)
    servos.write_us(1, 1800)
    servos.close()
    assert pins.last("pwm", 18) == 0.0 and pins.last("pwm", 19) == 0.0
    assert pins.closed is True


def test_gpio_servos_report_unavailable_without_gpiozero(monkeypatch):
    monkeypatch.setitem(sys.modules, "gpiozero", None)
    with pytest.raises(ServosUnavailable, match="servo pins"):
        GpioServos(SERVO_PINS, 50)


def test_no_servos_accepts_writes_and_stays_absent():
    """`--servos off` must not raise: main.py offers it, and scoop_pose then refuses."""
    cfg = parse_calib(helpers.CALIB_DICT).servos
    servos = no_servos(cfg)
    assert servos.present is False and servos.name == "none"
    servos.write_us(cfg.shoulder_ch, 1500)  # accepted and discarded
    servos.off(cfg.shoulder_ch)
    servos.close()
    assert servos.last_us(cfg.shoulder_ch) is None


def test_an_unwired_channel_and_no_servos_are_different_mistakes():
    """A real GpioServos must still reject a channel typo rather than silently ignore it."""
    servos, _ = gpio()
    with pytest.raises(ValueError, match="not wired"):
        servos.write_us(9, 1500)
    no_servos(parse_calib(helpers.CALIB_DICT).servos).write_us(9, 1500)  # no raise


def test_both_backends_satisfy_the_same_driver_surface():
    """scoop.py must never learn which one is underneath."""
    gpio_servos, _ = gpio()
    pca, _ = build()
    for driver in (gpio_servos, pca):
        for method in ("write_us", "off", "last_us", "close"):
            assert callable(getattr(driver, method)), f"{driver.name} lacks {method}"
        assert isinstance(driver.present, bool)
    assert gpio_servos.name == "gpio" and pca.name == "pca9685"
