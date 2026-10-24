"""Exclusion radius around visited spots (doc 00 §8): never re-sample a spot."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

from marsrover_shared.schemas import Candidate, Visit

OPEN = ("new", "queued")


def excluded(
    xy: tuple[float, float], visits: Iterable[Visit], radius_m: float | None = None
) -> bool:
    """True if `xy` is within a visit's radius (its own radius_m, or the override)."""
    x, y = xy
    return any(math.hypot(v.x - x, v.y - y) <= (radius_m or v.radius_m) for v in visits)


def newly_excluded(cands: Mapping[str, Candidate], visits: Iterable[Visit]) -> list[str]:
    """Open candidates (new/queued) that now fall inside a visit and must be marked excluded."""
    visits = list(visits)
    return [cid for cid, c in cands.items() if c.status in OPEN and excluded((c.x, c.y), visits)]
