"""Frame helpers (doc 00 §4).

Mission frame M: origin = home pad, +x = rover heading at run start, +y = left,
heading CCW from +x in degrees on (-180, 180]. Rover frame R: +x forward, +y left.
"""

from __future__ import annotations

import math
from typing import Protocol


class _PoseLike(Protocol):
    x: float
    y: float
    heading_deg: float


def wrap_deg(a: float) -> float:
    """Wrap an angle in degrees to (-180, 180]."""
    r = math.fmod(a, 360.0)
    if r <= -180.0:
        r += 360.0
    elif r > 180.0:
        r -= 360.0
    return r


def rover_to_mission(pose: _PoseLike, xy_r: tuple[float, float]) -> tuple[float, float]:
    """A point in the rover frame at `pose` → the mission frame."""
    h = math.radians(pose.heading_deg)
    c, s = math.cos(h), math.sin(h)
    xr, yr = xy_r
    return (pose.x + c * xr - s * yr, pose.y + s * xr + c * yr)


def mission_to_rover(pose: _PoseLike, xy_m: tuple[float, float]) -> tuple[float, float]:
    """A mission-frame point → the rover frame at `pose`."""
    h = math.radians(pose.heading_deg)
    c, s = math.cos(h), math.sin(h)
    dx, dy = xy_m[0] - pose.x, xy_m[1] - pose.y
    return (c * dx + s * dy, -s * dx + c * dy)
