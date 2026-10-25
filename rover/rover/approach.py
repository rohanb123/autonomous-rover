"""The `approach` op's geometry: turn to face a target, then close to close-up range.

The brain's FSM already drives to within 0.25 m of the target and faces it
(gx10/brain/fsm.py `_go_to_near`); this is the last bit, from the Pi's own pose.

# UNVERIFIED: dead reckoning only, no visual servo yet (doc 00 section 1 step 1 wants one).
# The target is where the station's BEV put it, so `offset_px` is always [0, 0] and
# `in_zone` means "the pose says we are there", not "the camera sees it centred".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from marsrover_shared.frames import wrap_deg
from marsrover_shared.schemas import Pose

#: Rover centre to target for the close-up, metres. The survey focus band starts at 0.3 m,
#: so nearer than that the rock is likely under the camera's field of view.
# UNVERIFIED: measure where the CM3's lower frame edge meets the ground on our chassis.
CLOSEUP_STANDOFF_M = 0.30
#: A target further than this from the current pose is not an approach, it is a drive the
#: brain should have made first: answer in_zone=false rather than wander.
MAX_APPROACH_M = 1.0
#: Back up at most this far to open the distance to a target that is too close.
MAX_REVERSE_M = 0.15
#: Below this the turn is skipped; the drivetrain cannot resolve it anyway.
MIN_TURN_DEG = 2.0
#: How close to the standoff counts as in the zone.
ZONE_TOL_M = 0.05


@dataclass(frozen=True)
class ApproachPlan:
    turn_deg: float  # CCW positive; 0.0 when below MIN_TURN_DEG
    drive_m: float  # negative = reverse
    reachable: bool  # False: out of range, nothing should move


def plan_approach(
    pose: Pose, x: float, y: float, standoff_m: float = CLOSEUP_STANDOFF_M
) -> ApproachPlan:
    """From `pose`, the turn and drive that leave (x, y) `standoff_m` straight ahead."""
    dx, dy = x - pose.x, y - pose.y
    dist = math.hypot(dx, dy)
    if dist > MAX_APPROACH_M:
        return ApproachPlan(0.0, 0.0, reachable=False)
    turn = wrap_deg(math.degrees(math.atan2(dy, dx)) - pose.heading_deg) if dist > 1e-6 else 0.0
    if abs(turn) < MIN_TURN_DEG:
        turn = 0.0
    drive = max(dist - standoff_m, -MAX_REVERSE_M)
    return ApproachPlan(turn, drive, reachable=True)


def in_zone(pose: Pose, x: float, y: float, standoff_m: float = CLOSEUP_STANDOFF_M) -> bool:
    """True when the target is about `standoff_m` away (heading is not checked)."""
    return abs(math.hypot(x - pose.x, y - pose.y) - standoff_m) <= ZONE_TOL_M
