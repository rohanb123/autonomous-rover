"""Tests for rover/rover/imu.py.

The fake buses in helpers.py hold **real synthetic register bytes**, so these exercise the
actual two's-complement decode and LSB scaling rather than a second fake of the arithmetic.
Getting that wrong is not a crash: it is a heading that drifts, which is docs/05 R2.

The assertions that earn their keep:
  * **Scaling against the datasheet**, both signs, for both chips.
  * **`has_gyro()`**, because it is the single switch between a closed-loop and a timed turn
    (docs/05 U-2), and `read()` on an accel-only part must have *no* `gz_dps` key at all.
  * **The MPU is probed before the ADXL**, so the better part wins if both answer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402

from rover.imu import (  # noqa: E402
    ADXL_ADDR,
    ADXL_DATA_FORMAT,
    ADXL_POWER_CTL,
    MPU_ADDR,
    MPU_CONFIG,
    MPU_DLPF_44HZ,
    MPU_PWR_MGMT_1,
    MPU_WHO_AM_I,
    MPU_WHO_AM_I_VALUES,
    NONE,
    G,
    Imu,
    be_signed,
    le_signed,
)

# --- word decoding ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hi, lo, expected",
    [
        (0x00, 0x00, 0),
        (0x40, 0x00, 16384),  # +1 g at +/-2 g
        (0x7F, 0xFF, 32767),
        (0xC0, 0x00, -16384),  # -1 g
        (0xFF, 0xFF, -1),
        (0x80, 0x00, -32768),
    ],
)
def test_be_signed(hi: int, lo: int, expected: int):
    assert be_signed(hi, lo) == expected


@pytest.mark.parametrize(
    "lo, hi, expected",
    [(0x00, 0x01, 256), (0x00, 0xFF, -256), (0xFF, 0xFF, -1), (0x00, 0x00, 0)],
)
def test_le_signed(lo: int, hi: int, expected: int):
    assert le_signed(lo, hi) == expected


# --- detection ----------------------------------------------------------------------------


def test_an_mpu6050_is_detected_and_reports_a_gyro():
    imu = Imu(helpers.fake_mpu_bus())
    assert imu.detect() == "mpu6050@0x68"
    assert imu.has_gyro() is True
    assert imu.present is True


def test_an_adxl345_is_detected_and_reports_no_gyro():
    imu = Imu(helpers.fake_adxl_bus())
    assert imu.detect() == "adxl345@0x53"
    assert imu.has_gyro() is False, "accel only, so turns must stay timed (docs/05 U-2)"
    assert imu.present is True


def test_an_empty_bus_is_none_not_an_exception():
    """No IMU is a degraded rover, not a crashed one: timed turns, tilt cutoff off."""
    imu = Imu(helpers.FakeBus({}))
    assert imu.detect() == NONE
    assert imu.has_gyro() is False and imu.present is False
    assert imu.read() == {}


def test_no_bus_at_all_is_none():
    imu = Imu(None)
    assert imu.detect() == NONE and imu.read() == {}


@pytest.mark.parametrize("who_am_i", sorted(MPU_WHO_AM_I_VALUES))
def test_the_common_gy521_clones_are_accepted(who_am_i: int):
    """The datasheet says 0x68; real GY-521 boards answer 0x70/0x72/0x98."""
    bus = helpers.fake_mpu_bus()
    bus.devices[MPU_ADDR][MPU_WHO_AM_I] = who_am_i
    assert Imu(bus).detect() == "mpu6050@0x68"


def test_an_unrecognised_chip_is_not_claimed():
    bus = helpers.fake_mpu_bus()
    bus.devices[MPU_ADDR][MPU_WHO_AM_I] = 0x42
    assert Imu(bus).detect() == NONE


def test_the_mpu_wins_when_both_answer():
    """Probe order matters: the part with a gyro is strictly better for R2."""
    both = helpers.FakeBus(
        {
            **helpers.fake_mpu_bus().devices,
            **helpers.fake_adxl_bus().devices,
        }
    )
    assert Imu(both).detect() == "mpu6050@0x68"
    assert Imu(both, addrs=(ADXL_ADDR,)).detect() == "adxl345@0x53", "addrs narrows the probe"


def test_a_chip_that_answers_but_will_not_configure_is_skipped():
    class Deaf(helpers.FakeBus):
        def write8(self, addr: int, reg: int, value: int) -> None:
            raise OSError(121, "Remote I/O error")

    assert Imu(Deaf(helpers.fake_mpu_bus().devices)).detect() == NONE


# --- configuration ------------------------------------------------------------------------


def test_the_mpu_is_woken_and_low_pass_filtered():
    bus = helpers.fake_mpu_bus()
    Imu(bus)
    writes = {reg: value for _addr, reg, value in bus.writes}
    assert writes[MPU_PWR_MGMT_1] != 0x40, "must come out of sleep or every read is zero"
    # docs/01 section 8: the IMU sits near the motors, and unfiltered buzz integrates
    # straight into heading error.
    assert writes[MPU_CONFIG] == MPU_DLPF_44HZ


def test_the_adxl_leaves_standby_in_full_resolution():
    bus = helpers.fake_adxl_bus()
    Imu(bus)
    writes = {reg: value for _addr, reg, value in bus.writes}
    assert writes[ADXL_POWER_CTL] & 0x08, "the Measure bit, or it stays in standby"
    assert writes[ADXL_DATA_FORMAT] & 0x08, "full resolution keeps 256 LSB/g flat"


# --- scaling ------------------------------------------------------------------------------


def test_the_mpu_scales_accel_to_metres_per_second_squared():
    """doc 00 section 6.2's telemetry example reads az 9.79, so m/s^2 is the contract."""
    imu = Imu(helpers.fake_mpu_bus(ax_g=0.0, ay_g=0.0, az_g=1.0))
    sample = imu.read()
    assert sample["az"] == pytest.approx(G, abs=1e-3)
    assert sample["ax"] == pytest.approx(0.0, abs=1e-3)


@pytest.mark.parametrize("g", [1.0, -1.0, 0.5, -0.25, 1.9])
def test_the_mpu_accel_sign_and_magnitude_round_trip(g: float):
    imu = Imu(helpers.fake_mpu_bus(ax_g=g, az_g=0.0))
    assert imu.read()["ax"] == pytest.approx(g * G, abs=1e-2)


def test_an_over_range_reading_saturates_rather_than_flipping_sign():
    """+/-2 g is the configured full scale. A real sensor pins at 0x7FFF; the decode must
    not wrap, or a hard knock would read as acceleration the *other* way."""
    imu = Imu(helpers.fake_mpu_bus(ax_g=5.0, az_g=0.0))
    ax = imu.read()["ax"]
    assert ax > 0, "saturated, not wrapped"
    assert ax == pytest.approx(2.0 * G, abs=0.01)


@pytest.mark.parametrize("dps", [0.0, 90.0, -90.0, 250.0, -249.0, 1.5])
def test_the_mpu_gyro_scales_at_131_lsb_per_dps(dps: float):
    imu = Imu(helpers.fake_mpu_bus(gz_dps=dps))
    assert imu.read()["gz_dps"] == pytest.approx(dps, abs=0.02)


def test_the_adxl_scales_at_256_lsb_per_g_and_omits_the_gyro():
    imu = Imu(helpers.fake_adxl_bus(ax_g=-1.0, az_g=1.0))
    sample = imu.read()
    assert sample["ax"] == pytest.approx(-G, abs=1e-2)
    assert sample["az"] == pytest.approx(G, abs=1e-2)
    assert "gz_dps" not in sample, "no key at all, so callers cannot read a fake zero"


def test_a_short_read_is_an_oserror_not_silent_garbage():
    class Truncating(helpers.FakeBus):
        def read_block(self, addr: int, reg: int, length: int) -> list[int]:
            return super().read_block(addr, reg, length)[:2]

    imu = Imu(Truncating(helpers.fake_mpu_bus().devices))
    with pytest.raises(OSError, match="short IMU read"):
        imu.read()


def test_close_drops_the_driver_and_the_bus():
    bus = helpers.fake_mpu_bus()
    imu = Imu(bus)
    imu.close()
    assert imu.present is False and imu.detect() == NONE
    assert imu.read() == {} and bus.closed is True
