"""Station plan, top-k heap and nearest-neighbour route (doc 00 §8)."""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterable, Mapping

from brain import settings
from marsrover_shared.schemas import Candidate

OPEN = ("new", "queued")


def station_plan(radius_m: float) -> list[tuple[float, float]]:
    """Home, plus 4 points at 0.6·radius (+x, +y, −x, −y) if radius > 0.8 m."""
    plan = [(0.0, 0.0)]
    if radius_m > settings.load().extra_stations_if_radius_gt_m:
        r = 0.6 * radius_m
        plan += [(r, 0.0), (0.0, r), (-r, 0.0), (0.0, -r)]
    return plan


class TopK:
    """Min-heap of (score, cand_id) capped at k: the root is the weakest of the top-k."""

    def __init__(self, k: int) -> None:
        self.k = k
        self._heap: list[tuple[float, str]] = []

    def __len__(self) -> int:
        return len(self._heap)

    def __contains__(self, cand_id: str) -> bool:
        return any(cid == cand_id for _, cid in self._heap)

    def offer(self, cand_id: str, score: float) -> None:
        if cand_id in self:  # re-scored (e.g. after a merge): replace its entry
            self._heap = [(s, cid) for s, cid in self._heap if cid != cand_id]
            heapq.heapify(self._heap)
        if len(self._heap) < self.k:
            heapq.heappush(self._heap, (score, cand_id))
        elif (score, cand_id) > self._heap[0]:
            heapq.heapreplace(self._heap, (score, cand_id))

    def rebuild(self, cands: Mapping[str, Candidate]) -> None:
        """After merge/skip/exclusion: the k best open candidates."""
        best = heapq.nlargest(
            self.k, ((c.score, cid) for cid, c in cands.items() if c.status in OPEN)
        )
        self._heap = best
        heapq.heapify(self._heap)

    def items(self) -> list[tuple[float, str]]:
        """Best first; ties broken by cand_id so ranks are stable."""
        return sorted(self._heap, key=lambda t: (-t[0], t[1]))

    def ranks(self) -> dict[str, int]:
        return {cid: i + 1 for i, (_, cid) in enumerate(self.items())}


def nn_route(start: tuple[float, float], targets: Iterable[Candidate]) -> list[Candidate]:
    """Greedy nearest-neighbour order from `start`. Recompute after every visit."""
    left = list(targets)
    route: list[Candidate] = []
    x, y = start
    while left:
        nxt = min(left, key=lambda c: (math.hypot(c.x - x, c.y - y), c.cand_id))
        route.append(nxt)
        left.remove(nxt)
        x, y = nxt.x, nxt.y
    return route
