"""Can the scoop pick this up? (docs/roles/P3 §4, docs/01 §3: pebbles 1.5-3 cm, bucket ~5 cm.)

1.0 for sizes in [0.012, 0.030] m, falling linearly to 0 at 0.005 m and at 0.045 m.
`size_m` is the Pi's estimate (docs/00 §6.4 proposals[].size_m); this module just trusts it.
"""

from __future__ import annotations

import math

FULL_MIN_M, FULL_MAX_M = 0.012, 0.030
ZERO_MIN_M, ZERO_MAX_M = 0.005, 0.045


def pickable(size_m: float, solidity: float | None = None) -> float:
    """Score in [0, 1]. `solidity` is accepted for the role-doc signature but not used yet."""
    if size_m is None or not math.isfinite(size_m):
        return 0.0
    if FULL_MIN_M <= size_m <= FULL_MAX_M:
        return 1.0
    if ZERO_MIN_M < size_m < FULL_MIN_M:
        return (size_m - ZERO_MIN_M) / (FULL_MIN_M - ZERO_MIN_M)
    if FULL_MAX_M < size_m < ZERO_MAX_M:
        return (ZERO_MAX_M - size_m) / (ZERO_MAX_M - FULL_MAX_M)
    return 0.0
