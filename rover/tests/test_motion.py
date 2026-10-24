"""Tests for rover/rover/motion.py and rover/rover/pose.py.

Every timing path runs in zero real time: `Motion` takes `clock` and `sleep` by injection, and
`helpers.FakeClock.sleep` advances the clock instead of blocking.

The assertion worth reading twice is `test_no_wheel_command_survives_an_inhibit`. That is the
re-enable race from docs/05 R14: the worker thread calls `set()` every 20 ms, so a stop that
does not latch is undone almost immediately.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402

from marsrover_shared.schemas import Pose  # noqa: E402
from rover.config import interp, parse_calib  # noqa: E402
from rover.motion import Aborted, Motion  # noqa: E402
from rover.motors import Motors, RecordingPins  # noqa: E402
from rover.pose import PoseEstimator  # noqa: E402

CALIB = parse_calib(helpers.CALIB_DICT)
PINS = CALIB.pins


class FakeImu:
    """A gyro that always reads `gz_dps`, or no gyro at all."""

    def __init__(self, gz_dps: float = 0.0, gyro: bool = True) -> None:
        self.gz_dps = gz_dps
        self._gyro = gyro

    def has_gyro(self) -> bool:
        return self._gyro

    def read(self) -> dict[str, float]:
        return {"ax": 0.0, "ay": 0.0, "az": 9.81, "gz_dps": self.gz_dps}


class FakeSafety:
    def __init__(self, latched: bool = False) -> None:
        self._latched = latched

    def latched(self) -> bool:
        return self._latched


def first(pins: RecordingPins, kind: str, pin: int) -> float | bool | None:
    """The first write of `kind` to `pin`. Motors.set() writes STBY before the wheels, so
    positional indexing into `writes` is fragile; name what you mean instead."""
    for k, p, value in pins.writes:
        if k == kind and p == pin:
            return value
    return None


def build(imu=None, safety=None, calib=CALIB):
    clock = helpers.FakeClock()
    pins = RecordingPins()
    motors = Motors(calib.pins, calib.motor_trim, pins)
    pose = PoseEstimator(calib.pose)
    motion = Motion(motors, pose, calib, imu=imu, safety=safety, clock=clock, sleep=clock.sleep)
    return motion, motors, pins, pose, clock


# --- pose ----------------------------------------------------------------------------------


def test_pose_starts_at_home_facing_plus_x():
    pose = PoseEstimator(CALIB.pose)
    p = pose.current()
    assert (p.x, p.y, p.heading_deg) == (0.0, 0.0, 0.0)
    assert p.source == "dr"


def test_predict_drive_follows_the_heading():
    pose = PoseEstimator(CALIB.pose)
    pose.predict_turn(90.0)
    p = pose.predict_drive(0.5)
    assert p.x == pytest.approx(0.0, abs=1e-9)
    assert p.y == pytest.approx(0.5), "+y is left, so facing 90 deg drives towards +y"


def test_predict_drive_backwards():
    pose = PoseEstimator(CALIB.pose)
    assert pose.predict_drive(-0.3).x == pytest.approx(-0.3)


@pytest.mark.parametrize("turn, expected", [(270.0, -90.0), (-270.0, 90.0), (180.0, 180.0)])
def test_headings_wrap_into_the_contract_range(turn: float, expected: float):
    pose = PoseEstimator(CALIB.pose)
    p = pose.predict_turn(turn)
    assert p.heading_deg == pytest.approx(expected)
    assert -180.0 < p.heading_deg <= 180.0


def test_sigma_grows_with_distance_and_saturates():
    pose = PoseEstimator(CALIB.pose)
    start = pose.current().sigma_xy_m
    pose.predict_drive(1.0)
    grown = pose.current().sigma_xy_m
    assert grown is not None and start is not None and grown > start
    pose.predict_drive(100.0)
    assert pose.current().sigma_xy_m == pytest.approx(CALIB.pose.sigma_xy_max_m)


def test_set_pose_resets_the_drift_and_keeps_the_source():
    pose = PoseEstimator(CALIB.pose)
    pose.predict_drive(5.0)
    pose.set_pose(Pose(x=1.0, y=-0.5, heading_deg=400.0, source="aruco", sigma_xy_m=0.02))
    p = pose.current()
    assert (p.x, p.y) == (1.0, -0.5)
    assert p.heading_deg == pytest.approx(40.0), "set_pose wraps too"
    assert p.source == "aruco" and p.sigma_xy_m == pytest.approx(0.02)
    pose.predict_drive(0.1)
    assert pose.current().sigma_xy_m == pytest.approx(CALIB.pose.sigma_xy_m0 + 0.05 * 0.1)


def test_the_source_reports_whether_turns_are_gyro_closed():
    pose = PoseEstimator(CALIB.pose)
    assert pose.predict_drive(0.1).source == "dr"
    pose.use_gyro(True)
    assert pose.predict_drive(0.1).source == "dr+gyro"


# --- drive ---------------------------------------------------------------------------------


def test_drive_runs_for_the_calibrated_duration():
    motion, _, _, _, clock = build()
    v = interp(CALIB.m_per_s_at, 0.5)
    pose = motion.drive(0.3, 0.5)
    assert clock.t == pytest.approx(0.3 / v, abs=1e-6)
    assert pose.x == pytest.approx(0.3, abs=1e-6)


def test_drive_backwards_goes_backwards():
    motion, _, pins, _, _ = build()
    pose = motion.drive(-0.2, 0.5)
    assert pose.x == pytest.approx(-0.2, abs=1e-6)
    assert first(pins, "out", PINS.ain1) is False, "AIN1 low, AIN2 high is reverse"
    assert first(pins, "out", PINS.ain2) is True


def test_drive_ends_stopped():
    motion, _, pins, _, _ = build()
    motion.drive(0.2, 0.5)
    assert pins.last("pwm", PINS.pwm_a) == 0.0
    assert pins.last("out", PINS.stby) is False


def test_speed_is_clamped_to_the_usable_band():
    """Below min_speed the gearmotors buzz instead of turning, so asking for 0.01 gets 0.15."""
    motion, _, pins, _, _ = build()
    motion.drive(0.1, 0.01)
    assert first(pins, "pwm", PINS.pwm_a) == pytest.approx(CALIB.motion.min_speed)


def test_a_cooperative_stop_shortens_the_drive_without_an_error():
    motion, _, _, _, clock = build()
    v = interp(CALIB.m_per_s_at, 0.5)

    original = clock.sleep
    calls = {"n": 0}

    def sleep_then_stop(dt: float) -> None:
        original(dt)
        calls["n"] += 1
        if calls["n"] == 3:
            motion.request_stop()  # what the WS server does on a `stop` frame

    motion.sleep = sleep_then_stop
    pose = motion.drive(1.0, 0.5)  # would take 5 s; we cut it after 3 ticks
    assert pose.x == pytest.approx(v * clock.t, abs=1e-9)
    assert pose.x < 1.0, "the drive was cut short"


def test_an_estop_latch_aborts_the_drive_with_e_estop():
    safety = FakeSafety(latched=False)
    motion, _, _, _, clock = build(safety=safety)
    original = clock.sleep

    def sleep_then_latch(dt: float) -> None:
        original(dt)
        safety._latched = True

    motion.sleep = sleep_then_latch
    with pytest.raises(Aborted) as exc:
        motion.drive(1.0, 0.5)
    assert exc.value.code == "E_ESTOP"
    assert "of 1.000 m" in exc.value.msg


def test_a_watchdog_latch_aborts_the_drive_with_e_timeout():
    motion, motors, _, _, clock = build()
    original = clock.sleep

    def sleep_then_inhibit(dt: float) -> None:
        original(dt)
        motors.inhibit("watchdog")

    motion.sleep = sleep_then_inhibit
    with pytest.raises(Aborted) as exc:
        motion.drive(1.0, 0.5)
    assert exc.value.code == "E_TIMEOUT"


def test_a_tilt_latch_aborts_the_drive_with_e_tilt():
    motion, motors, _, _, clock = build()
    original = clock.sleep

    def sleep_then_inhibit(dt: float) -> None:
        original(dt)
        motors.inhibit("tilt")

    motion.sleep = sleep_then_inhibit
    with pytest.raises(Aborted) as exc:
        motion.drive(1.0, 0.5)
    assert exc.value.code == "E_TILT"


def test_no_wheel_command_survives_an_inhibit():
    """docs/05 R14: the worker calls set() every 20 ms, so a stop must latch to hold."""
    motion, motors, pins, _, clock = build()
    original = clock.sleep

    def sleep_then_cut(dt: float) -> None:
        original(dt)
        motors.inhibit("estop")
        motors.stop()
        pins.clear()  # from here on, nothing may drive the wheels again

    motion.sleep = sleep_then_cut
    with pytest.raises(Aborted):
        motion.drive(1.0, 0.5)
    driving = [w for w in pins.writes if w[0] == "pwm" and w[2] != 0.0]
    assert driving == [], f"the wheels were re-commanded after the cut: {driving}"


def test_the_partial_pose_reflects_where_it_actually_got_to():
    motion, _, _, pose, clock = build()
    v = interp(CALIB.m_per_s_at, 0.5)
    original = clock.sleep
    calls = {"n": 0}

    def sleep_then_stop(dt: float) -> None:
        original(dt)
        calls["n"] += 1
        if calls["n"] == 5:
            motion.request_stop()

    motion.sleep = sleep_then_stop
    motion.drive(2.0, 0.5)
    assert pose.current().x == pytest.approx(v * clock.t, abs=1e-9)


# --- turn ----------------------------------------------------------------------------------


def test_turn_without_a_gyro_is_timed():
    motion, _, _, _, clock = build(imu=FakeImu(gyro=False))
    pose, method = motion.turn(90.0, 0.5)
    assert method == "timed"
    w = interp(CALIB.deg_per_s_at, 0.5)
    assert clock.t == pytest.approx(90.0 / w, abs=1e-6)
    assert pose.heading_deg == pytest.approx(90.0, abs=1e-6)


def test_turn_with_no_imu_at_all_is_timed():
    """docs/05 U-2's fallback: no IMU in the kit means every turn is open-loop."""
    motion, _, _, _, _ = build(imu=None)
    _, method = motion.turn(45.0, 0.5)
    assert method == "timed"


def test_a_timed_turn_spins_the_wheels_opposite_ways():
    motion, _, pins, _, _ = build()
    motion.turn(90.0, 0.5)
    # CCW positive: the left wheel reverses while the right drives forward.
    assert first(pins, "out", PINS.ain1) is False
    assert first(pins, "out", PINS.ain2) is True
    assert first(pins, "out", PINS.bin1) is True
    assert first(pins, "out", PINS.bin2) is False


def test_turn_with_a_gyro_closes_the_loop():
    motion, _, _, _, clock = build(imu=FakeImu(gz_dps=90.0))
    pose, method = motion.turn(90.0, 0.5)
    assert method == "gyro"
    assert pose.heading_deg == pytest.approx(90.0, abs=1.0)
    # 90 deg at 90 deg/s is about a second, far short of the ceiling.
    assert clock.t < 2.0


def test_negative_turns_go_clockwise():
    motion, _, _, _, _ = build(imu=FakeImu(gz_dps=90.0))
    pose, _ = motion.turn(-90.0, 0.5)
    assert pose.heading_deg == pytest.approx(-90.0, abs=1.0)


def test_a_dead_gyro_hits_the_ceiling_instead_of_spinning_forever():
    """A gyro reading 0 would otherwise turn until the heartbeat watchdog noticed."""
    motion, _, pins, _, clock = build(imu=FakeImu(gz_dps=0.0))
    pose, method = motion.turn(90.0, 0.5)
    assert method == "gyro"
    w = interp(CALIB.deg_per_s_at, 0.5)
    ceiling = (90.0 / w) * 2.5 + 1.0
    assert clock.t == pytest.approx(ceiling, abs=0.05)
    assert pose.heading_deg == pytest.approx(0.0, abs=1e-6), "it never actually turned"
    assert pins.last("out", PINS.stby) is False


def test_gyro_turn_gain_scales_the_integration():
    calib = parse_calib(helpers.calib_dict(motion={"gyro_turn_gain": 2.0}))
    motion, _, _, _, clock = build(imu=FakeImu(gz_dps=90.0), calib=calib)
    motion.turn(90.0, 0.5)
    assert clock.t == pytest.approx(0.5, abs=0.05), "twice the gain, half the time"


def test_an_estop_mid_gyro_turn_aborts():
    safety = FakeSafety()
    motion, _, _, _, clock = build(imu=FakeImu(gz_dps=10.0), safety=safety)
    original = clock.sleep

    def sleep_then_latch(dt: float) -> None:
        original(dt)
        safety._latched = True

    motion.sleep = sleep_then_latch
    with pytest.raises(Aborted) as exc:
        motion.turn(180.0, 0.5)
    assert exc.value.code == "E_ESTOP"


# --- stop ----------------------------------------------------------------------------------


def test_stop_halts_and_reports_the_current_pose():
    motion, _, pins, pose, _ = build()
    motion.drive(0.2, 0.5)
    pins.clear()
    reported = motion.stop()
    assert reported == pose.current()
    assert pins.last("out", PINS.stby) is False
    assert motion.stopping is True
    motion.clear_stop()
    assert motion.stopping is False


def test_wrapped_delta_takes_the_short_way_round():
    motion, _, _, pose, _ = build()
    pose.predict_turn(170.0)
    assert motion.wrapped_delta(-170.0) == pytest.approx(20.0)


def test_a_zero_speed_table_is_an_internal_error_not_a_hang():
    calib = parse_calib(helpers.calib_dict(motion={"m_per_s_at": {0.5: 0.0}}))
    motion, _, _, _, _ = build(calib=calib)
    with pytest.raises(Aborted) as exc:
        motion.drive(1.0, 0.5)
    assert exc.value.code == "E_INTERNAL"
