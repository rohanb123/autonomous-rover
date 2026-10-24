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
import time
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
    ImuPoller,
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


# --- gyro bias (docs/05 R2: a stationary bias is pure heading error) ----------------------


def biased(gz_dps: float = 0.0, **kwargs) -> tuple[Imu, helpers.FakeClock]:
    """An MPU reading a constant `gz_dps`, on a clock the test drives."""
    clock = helpers.FakeClock()
    imu = Imu(helpers.fake_mpu_bus(gz_dps=gz_dps), clock=clock, sleep=clock.sleep, **kwargs)
    return imu, clock


@pytest.mark.parametrize("bias", [0.0, 1.4, -2.1, 0.08])
def test_a_constant_bias_is_measured_and_then_subtracted(bias: float):
    imu, _ = biased(bias)
    assert imu.read()["gz_dps"] == pytest.approx(bias, abs=0.02)
    measured = imu.gyro_bias_calibrate(2.0)
    assert measured == pytest.approx(bias, abs=0.02)
    # From here on a still rover reads zero, which is the whole point.
    assert imu.read()["gz_dps"] == pytest.approx(0.0, abs=0.02)


def test_calibration_takes_the_time_it_was_asked_for():
    imu, clock = biased(1.0)
    imu.gyro_bias_calibrate(2.0)
    assert clock.t == pytest.approx(2.0, abs=0.05)


def test_recalibrating_does_not_correct_a_correction():
    """The second pass must measure the raw sensor, not the already-corrected reading."""
    imu, _ = biased(3.0)
    assert imu.gyro_bias_calibrate(1.0) == pytest.approx(3.0, abs=0.02)
    assert imu.gyro_bias_calibrate(1.0) == pytest.approx(3.0, abs=0.02)
    assert imu.gyro_bias_dps == pytest.approx(3.0, abs=0.02)


def test_an_accel_only_part_reports_no_bias_and_does_not_hang():
    clock = helpers.FakeClock()
    imu = Imu(helpers.fake_adxl_bus(), clock=clock, sleep=clock.sleep)
    assert imu.gyro_bias_calibrate(5.0) == 0.0
    assert clock.t == 0.0, "nothing to sample, so no time spent"


def test_zero_seconds_collects_nothing_and_keeps_the_old_bias():
    imu, _ = biased(2.0)
    imu.gyro_bias_calibrate(1.0)
    before = imu.gyro_bias_dps
    assert imu.gyro_bias_calibrate(0.0) == pytest.approx(before)
    assert imu.gyro_bias_dps == pytest.approx(before), "must not silently zero the bias"


def test_a_noisy_calibration_warns_that_the_rover_was_probably_moving(caplog):
    """A wide spread means it was not stationary, so the bias is not trustworthy."""

    class Swinging(helpers.FakeBus):
        """Alternates +50 and -50 deg/s, as if someone was turning it by hand."""

        def __init__(self, devices):
            super().__init__(devices)
            self._flip = False

        def read_block(self, addr: int, reg: int, length: int) -> list[int]:
            self._flip = not self._flip
            dps = 50.0 if self._flip else -50.0
            raw = helpers.fake_mpu_bus(gz_dps=dps).devices[MPU_ADDR]
            return [raw.get(reg + i, 0) for i in range(length)]

    clock = helpers.FakeClock()
    imu = Imu(Swinging(helpers.fake_mpu_bus().devices), clock=clock, sleep=clock.sleep)
    with caplog.at_level("WARNING"):
        bias = imu.gyro_bias_calibrate(0.5)
    assert bias == pytest.approx(0.0, abs=5.0), "the swings average out, which is the trap"
    assert any("spread" in r.message for r in caplog.records)


def test_a_bus_that_fails_throughout_gives_up_rather_than_spinning(caplog):
    class Flaky(helpers.FakeBus):
        def read_block(self, addr: int, reg: int, length: int) -> list[int]:
            raise OSError(121, "Remote I/O error")

    clock = helpers.FakeClock()
    imu = Imu(helpers.fake_mpu_bus(), clock=clock, sleep=clock.sleep)
    imu.bus = Flaky(imu.bus.devices)
    imu._driver.bus = imu.bus  # type: ignore[union-attr]
    with caplog.at_level("WARNING"):
        assert imu.gyro_bias_calibrate(10.0) == 0.0
    assert clock.t < 1.0, "gave up early instead of retrying for the full ten seconds"
    assert any("gave up" in r.message for r in caplog.records)


# --- ImuPoller: keeping I2C off the event loop --------------------------------------------


def test_the_poller_caches_a_sample_before_the_thread_even_starts():
    """The first telemetry frame must not be empty, so start() takes one sample inline."""
    imu = Imu(helpers.fake_mpu_bus(az_g=1.0))
    poller = ImuPoller(imu, hz=50.0, sleep=lambda _s: None)
    assert poller.cached() is None
    poller.poll_once()
    assert poller.cached()["az"] == pytest.approx(G, abs=1e-2)
    assert poller.samples == 1


def test_cached_never_touches_the_bus():
    """That is the entire reason this class exists: the loop thread must not block on I2C."""
    imu = Imu(helpers.fake_mpu_bus())
    poller = ImuPoller(imu, sleep=lambda _s: None)
    poller.poll_once()
    reads_before = len(imu.bus.writes)

    class Exploding(helpers.FakeBus):
        def read_block(self, addr: int, reg: int, length: int) -> list[int]:
            raise AssertionError("cached() must not read the bus")

    imu.bus = Exploding({})
    assert poller.cached() is not None  # served from the cache, no bus access
    assert len(imu.bus.writes) == 0 and reads_before >= 0


def test_a_failed_read_leaves_the_last_good_sample_in_place():
    """A single dropped sample must not blank the tilt input mid-drive."""
    imu = Imu(helpers.fake_mpu_bus(az_g=1.0))
    poller = ImuPoller(imu, sleep=lambda _s: None)
    poller.poll_once()
    good = poller.cached()

    class Flaky(helpers.FakeBus):
        def read_block(self, addr: int, reg: int, length: int) -> list[int]:
            raise OSError(121, "Remote I/O error")

    imu._driver.bus = Flaky({})  # type: ignore[union-attr]
    assert poller.poll_once() is None
    assert poller.cached() == good
    assert poller.failures == 1 and poller.present is True


def test_a_good_read_clears_the_failure_streak():
    imu = Imu(helpers.fake_mpu_bus())
    poller = ImuPoller(imu, max_failures=3, sleep=lambda _s: None)
    poller.failures = 2
    poller.poll_once()
    assert poller.failures == 0


def test_the_poller_gives_up_after_too_many_consecutive_failures(caplog):
    """A yanked ribbon should degrade to "no IMU", not log a trace 50 times a second."""

    class Dead(helpers.FakeBus):
        def read_block(self, addr: int, reg: int, length: int) -> list[int]:
            raise OSError(121, "Remote I/O error")

    imu = Imu(helpers.fake_mpu_bus())
    imu._driver.bus = Dead({})  # type: ignore[union-attr]
    poller = ImuPoller(imu, max_failures=3, sleep=lambda _s: None)
    with caplog.at_level("WARNING"):
        for _ in range(6):
            poller.poll_once()
    assert poller.present is False, "downstream now sees no IMU: timed turns, tilt off"
    # One summary at the threshold, not one per failed read forever.
    assert sum("gave up" in r.message for r in caplog.records) == 1


def test_no_imu_means_start_does_nothing():
    poller = ImuPoller(Imu(helpers.FakeBus({})), sleep=lambda _s: None)
    poller.start()
    assert poller.cached() is None and poller.present is False
    poller.stop()  # must not raise with no thread


def test_the_thread_really_runs_and_stops():
    """One genuinely threaded test, since the whole point is that it is off the loop."""
    imu = Imu(helpers.fake_mpu_bus(az_g=1.0))
    poller = ImuPoller(imu, hz=200.0)
    poller.start()
    try:
        deadline = time.monotonic() + 2.0
        while poller.samples < 5 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert poller.samples >= 5, f"only {poller.samples} samples in 2 s"
        assert poller.cached()["az"] == pytest.approx(G, abs=1e-2)
    finally:
        poller.stop()
    settled = poller.samples
    time.sleep(0.05)
    assert poller.samples == settled, "stop() left the thread running"
