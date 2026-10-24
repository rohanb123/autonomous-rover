"""Tests for rover/rover/safety.py -- the watchdog, the E-STOP latch and the tilt cutoff.

The role doc names this file "watchdog timing with a fake clock", and that is exactly how it
works: `Safety.tick()` is the decision function and it takes its clock by injection, so every
timing assertion here is exact and there is not one real sleep in the file. The 20 Hz *loop*
that calls it lives in ws_server and is covered loosely by test_protocol.py's
test_drive_without_heartbeats_stops_after_one_second, which tolerates Windows' ~16 ms timer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402

from rover.config import parse_calib  # noqa: E402
from rover.motors import Motors, RecordingPins  # noqa: E402
from rover.safety import Safety, tilt_deg  # noqa: E402

PINS = parse_calib(helpers.CALIB_DICT).pins
TICK = 1.0 / 20.0  # the watchdog loop's period


def build(
    *, moving: bool = True, imu: dict | None = None, **kwargs
) -> tuple[Safety, Motors, RecordingPins, helpers.FakeClock, list[tuple[str, dict]]]:
    clock = helpers.FakeClock()
    pins = RecordingPins()
    motors = Motors(PINS, 0.0, pins)
    events: list[tuple[str, dict]] = []
    state = {"moving": moving, "imu": imu}
    safety = Safety(
        motors,
        moving=lambda: bool(state["moving"]),
        clock=clock,
        imu_read=(lambda: state["imu"]) if imu is not None else None,
        on_event=lambda kind, detail: events.append((kind, detail)),
        **kwargs,
    )
    safety.state = state  # type: ignore[attr-defined]  - test handle to flip inputs
    return safety, motors, pins, clock, events


def stops(pins: RecordingPins) -> int:
    """How many times stop() ran: each one ends with STBY driven low."""
    return sum(
        1 for kind, pin, value in pins.writes if kind == "out" and pin == PINS.stby and not value
    )


# --- heartbeat watchdog --------------------------------------------------------------------


def test_the_cut_lands_on_the_first_tick_past_one_second():
    safety, motors, pins, clock, events = build()
    safety.on_heartbeat()
    # Set the clock absolutely rather than accumulating: repeatedly adding 0.05 overshoots
    # 1.0 by one float ulp, which would make this assert about IEEE754 instead of the
    # contract. `i * TICK` is one multiply, and 20 * 0.05 is exactly 1.0.
    for i in range(1, 21):
        clock.t = i * TICK
        safety.tick()
    assert clock.t == 1.0
    assert stops(pins) == 0, "1.00 s is inside the budget, not past it"
    assert safety.take_cut() is None

    clock.t = 1.0 + TICK  # the first tick strictly past 1.0 s
    safety.tick()
    assert stops(pins) == 1
    assert motors.inhibited and motors.latches == {"watchdog"}
    assert [kind for kind, _ in events] == ["stall"]
    assert events[0][1]["reason"] == "heartbeat_watchdog"


def test_the_cut_code_is_e_timeout_and_handed_over_once():
    safety, _, _, clock, _ = build()
    safety.on_heartbeat()
    clock.advance(1.5)
    safety.tick()
    assert safety.take_cut() == "E_TIMEOUT"
    assert safety.take_cut() is None, "a cut is reported once, not every tick"
    assert safety.stop_reason() == "heartbeat_watchdog"


def test_a_heartbeat_postpones_the_cut():
    safety, _, pins, clock, _ = build()
    safety.on_heartbeat()
    clock.advance(0.9)
    safety.tick()
    assert stops(pins) == 0
    safety.on_heartbeat()  # the brain checked in just in time
    clock.advance(0.9)
    safety.tick()
    assert stops(pins) == 0


def test_the_cut_is_idempotent():
    safety, _, pins, clock, events = build()
    safety.on_heartbeat()
    clock.advance(1.5)
    for _ in range(10):
        clock.advance(TICK)
        safety.tick()
    assert stops(pins) == 1, "one cut, not one per tick"
    assert len(events) == 1


def test_the_next_heartbeat_releases_the_watchdog_latch():
    """A brain that reconnects should just resume, with no operator intervention."""
    safety, motors, _, clock, _ = build()
    safety.on_heartbeat()
    clock.advance(1.5)
    safety.tick()
    assert motors.inhibited
    safety.on_heartbeat()
    assert not motors.inhibited


def test_idling_never_trips_the_watchdog():
    """Doc 00 section 4 says "while moving"; a parked rover with no brain is fine."""
    safety, motors, pins, clock, events = build(moving=False)
    safety.on_heartbeat()
    clock.advance(60.0)
    for _ in range(5):
        clock.advance(TICK)
        safety.tick()
    assert stops(pins) == 0 and not motors.inhibited and events == []


def test_a_custom_timeout_is_honoured():
    safety, _, pins, clock, _ = build(heartbeat_timeout_s=0.25)
    safety.on_heartbeat()
    clock.advance(0.2)
    safety.tick()
    assert stops(pins) == 0
    clock.advance(0.1)
    safety.tick()
    assert stops(pins) == 1


# --- E-STOP --------------------------------------------------------------------------------


def test_estop_latches_before_it_stops():
    """Ordering is the whole point: inhibit first, so the worker's next set() is already dead."""
    safety, motors, pins, _, _ = build()
    safety.estop()
    assert safety.latched() and motors.latches == {"estop"}
    assert stops(pins) == 1
    pins.clear()
    motors.set(1.0, 1.0)  # what the worker thread would do 20 ms later
    assert pins.writes == []


def test_clear_releases_only_the_estop_latch():
    safety, motors, _, clock, _ = build()
    safety.on_heartbeat()
    clock.advance(1.5)
    safety.tick()  # watchdog latch
    safety.estop()  # estop latch
    assert motors.latches == {"estop", "watchdog"}
    safety.clear()
    assert not safety.latched()
    assert motors.latches == {"watchdog"}, "clear_estop must not silence the watchdog"
    safety.on_heartbeat()
    assert not motors.inhibited


def test_clear_drops_a_pending_cut():
    safety, _, _, clock, _ = build()
    safety.on_heartbeat()
    clock.advance(1.5)
    safety.tick()
    safety.clear()
    assert safety.take_cut() is None and safety.stop_reason() is None


# --- tilt ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ax, ay, az, expected",
    [
        (0.0, 0.0, 9.81, 0.0),  # level
        (9.81, 0.0, 0.0, 90.0),  # on its side
        (0.0, 0.0, -9.81, 180.0),  # upside down, via atan2 not asin
        (9.81, 0.0, 9.81, 45.0),
    ],
)
def test_tilt_deg_uses_atan2(ax: float, ay: float, az: float, expected: float):
    assert tilt_deg({"ax": ax, "ay": ay, "az": az}) == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize(
    "degrees, ok",
    [(0.0, True), (34.9, True), (35.0, True), (35.1, False), (90.0, False), (179.0, False)],
)
def test_tilt_ok_at_the_limit(degrees: float, ok: bool):
    import math

    g = 9.81
    radians = math.radians(degrees)
    imu = {"ax": g * math.sin(radians), "ay": 0.0, "az": g * math.cos(radians)}
    safety, _, _, _, _ = build(imu=imu)
    assert safety.tilt_ok(imu) is ok


def test_tilt_ok_is_true_when_we_cannot_tell():
    """No IMU, or an all-zero reading, is "unknown" -- never claim a check we aren't running."""
    safety, _, _, _, _ = build()
    assert safety.tilt_ok(None) is True
    assert safety.tilt_ok({}) is True
    assert safety.tilt_ok({"ax": 0.0, "ay": 0.0, "az": 0.0}) is True


def test_a_tilt_cut_fires_once_and_clears_with_hysteresis():
    level = {"ax": 0.0, "ay": 0.0, "az": 9.81}
    tipped = {"ax": 9.0, "ay": 0.0, "az": 3.0}  # ~72 deg
    safety, motors, pins, clock, events = build(imu=level, tilt_clear_ticks=3)
    safety.on_heartbeat()

    safety.state["imu"] = tipped  # type: ignore[attr-defined]
    for _ in range(4):
        clock.advance(TICK)
        safety.on_heartbeat()
        safety.tick()
    assert stops(pins) == 1, "one cut while it stays tipped"
    assert motors.latches == {"tilt"}
    assert [kind for kind, _ in events] == ["tilt"]
    assert events[0][1]["tilt_deg"] == pytest.approx(71.57, abs=0.1)
    assert safety.take_cut() == "E_TILT"

    safety.state["imu"] = level  # type: ignore[attr-defined]
    for _ in range(2):  # fewer than tilt_clear_ticks: a single bump must not clear it
        clock.advance(TICK)
        safety.tick()
    assert motors.latches == {"tilt"}
    clock.advance(TICK)
    safety.tick()
    assert not motors.inhibited, "three consecutive safe ticks release it"


def test_no_imu_means_the_tilt_cutoff_is_simply_off():
    safety, motors, pins, clock, events = build()
    safety.on_heartbeat()
    for _ in range(5):
        clock.advance(TICK)
        safety.on_heartbeat()
        safety.tick()
    assert stops(pins) == 0 and not motors.inhibited and events == []
