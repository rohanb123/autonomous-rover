"""Tests for rover/rover/approach.py -- the geometry behind the `approach` op."""

from __future__ import annotations

import pytest

from marsrover_shared.schemas import Pose
from rover.approach import (
    CLOSEUP_STANDOFF_M,
    MAX_REVERSE_M,
    in_zone,
    plan_approach,
)


def pose(x: float = 0.0, y: float = 0.0, h: float = 0.0) -> Pose:
    return Pose(x=x, y=y, heading_deg=h, source="dr")


def test_dead_ahead_drives_only_the_gap_to_the_standoff():
    plan = plan_approach(pose(), 0.8, 0.0)
    assert plan.reachable and plan.turn_deg == 0.0
    assert plan.drive_m == pytest.approx(0.8 - CLOSEUP_STANDOFF_M)


def test_target_to_the_left_turns_ccw_first():
    plan = plan_approach(pose(), 0.0, 0.6)
    assert plan.turn_deg == pytest.approx(90.0)
    assert plan.drive_m == pytest.approx(0.6 - CLOSEUP_STANDOFF_M)


def test_turn_is_relative_to_the_current_heading_and_wrapped():
    plan = plan_approach(pose(h=170.0), -0.5, -0.1)  # bearing about -168.7 deg
    assert -30.0 < plan.turn_deg < 30.0


def test_a_tiny_turn_is_skipped():
    plan = plan_approach(pose(), 0.6, 0.01)  # about 1 deg off
    assert plan.turn_deg == 0.0


def test_too_close_backs_up_but_never_far():
    plan = plan_approach(pose(), 0.05, 0.0)
    assert -MAX_REVERSE_M <= plan.drive_m < 0.0


def test_out_of_range_moves_nothing():
    plan = plan_approach(pose(), 2.0, 0.0)
    assert not plan.reachable and plan.drive_m == 0.0 and plan.turn_deg == 0.0


def test_in_zone_is_about_the_standoff():
    assert in_zone(pose(x=0.5 - CLOSEUP_STANDOFF_M), 0.5, 0.0)
    assert not in_zone(pose(), 0.5, 0.0)
