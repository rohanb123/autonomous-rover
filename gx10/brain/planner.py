"""Station plan, top-k heap and the visiting route (doc 00 §8)."""

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


MAX_EXACT = 8  # k ≤ 8 (doc 00 §6.1): Held-Karp is ~n²·2ⁿ ≈ 16k steps, instant


def best_route(
    start: tuple[float, float],
    targets: Iterable[Candidate],
    end: tuple[float, float] | None = (0.0, 0.0),
) -> list[Candidate]:
    """Shortest order to visit every target from `start`, finishing at `end` (home).

    Exact (Held-Karp dynamic programming over subsets) for up to MAX_EXACT targets; pass
    end=None for an open path (return_home=false). Ties go to the lower cand_ids, so the
    route is deterministic. Beyond MAX_EXACT it falls back to nn_route.
    """
    pts = sorted(targets, key=lambda c: c.cand_id)
    n = len(pts)
    if n <= 1:
        return pts
    if n > MAX_EXACT:
        return nn_route(start, pts)
    xy = [(c.x, c.y) for c in pts]

    def d(a: tuple[float, float], b: tuple[float, float]) -> float:
        return math.hypot(a[0] - b[0], a[1] - b[1])

    # cost[mask][j]: shortest path from start through the targets in `mask`, ending at j.
    full = (1 << n) - 1
    cost = [[math.inf] * n for _ in range(1 << n)]
    prev = [[-1] * n for _ in range(1 << n)]
    for j in range(n):
        cost[1 << j][j] = d(start, xy[j])
    for mask in range(1, 1 << n):
        for j in range(n):
            c = cost[mask][j]
            if c == math.inf or not mask & (1 << j):
                continue
            for k in range(n):
                if mask & (1 << k):
                    continue
                nxt, nc = mask | (1 << k), c + d(xy[j], xy[k])
                if nc < cost[nxt][k] - 1e-12:
                    cost[nxt][k], prev[nxt][k] = nc, j
    tail = [0.0 if end is None else d(xy[j], end) for j in range(n)]
    last = min(range(n), key=lambda j: (cost[full][j] + tail[j], j))
    order, mask = [], full
    while last != -1:
        order.append(last)
        last, mask = prev[mask][last], mask & ~(1 << last)
    return [pts[i] for i in reversed(order)]


def route_length(
    start: tuple[float, float], route: Iterable[Candidate], end: tuple[float, float] | None = None
) -> float:
    """Total path length start → route → end (end=None: stop at the last target)."""
    x, y = start
    total = 0.0
    for c in route:
        total += math.hypot(c.x - x, c.y - y)
        x, y = c.x, c.y
    if end is not None:
        total += math.hypot(end[0] - x, end[1] - y)
    return total
