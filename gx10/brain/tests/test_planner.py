import random

import pytest

from brain.planner import TopK, nn_route, station_plan


def test_station_plan():
    assert station_plan(0.8) == [(0.0, 0.0)]
    plan = station_plan(1.5)
    assert plan[0] == (0.0, 0.0) and len(plan) == 5
    assert plan[1] == pytest.approx((0.9, 0.0))


@pytest.mark.parametrize("seed", range(30))
def test_topk_keeps_k_best_under_any_insert_order(seed):
    rng = random.Random(seed)
    scores = {f"c_{i:04d}": rng.random() for i in range(25)}
    order = list(scores)
    rng.shuffle(order)
    top = TopK(3)
    for cid in order:
        top.offer(cid, scores[cid])
    want = sorted(scores, key=lambda c: -scores[c])[:3]
    assert [cid for _, cid in top.items()] == want
    assert top.ranks() == {cid: i + 1 for i, cid in enumerate(want)}


def test_topk_rescore_and_rebuild(cand):
    top = TopK(2)
    for cid, s in [("c_0001", 0.9), ("c_0002", 0.8), ("c_0003", 0.1)]:
        top.offer(cid, s)
    top.offer("c_0003", 0.95)  # c_0003 re-scored after a merge
    assert [cid for _, cid in top.items()] == ["c_0003", "c_0001"]
    cands = {
        "c_0001": cand("c_0001", score=0.9, status="excluded"),
        "c_0002": cand("c_0002", score=0.8),
        "c_0003": cand("c_0003", score=0.95, status="visited"),
        "c_0004": cand("c_0004", score=0.4, status="queued"),
    }
    top.rebuild(cands)
    assert [cid for _, cid in top.items()] == ["c_0002", "c_0004"]


def test_nn_route_is_greedy_nearest(cand):
    targets = [cand("c_far", x=2, y=0), cand("c_near", x=0.5, y=0), cand("c_mid", x=1.0, y=0.1)]
    assert [c.cand_id for c in nn_route((0, 0), targets)] == ["c_near", "c_mid", "c_far"]
    assert nn_route((0, 0), []) == []


def _brute(start, targets, end):
    import itertools

    from brain.planner import route_length

    return min(route_length(start, list(p), end) for p in itertools.permutations(targets))


@pytest.mark.parametrize("seed", range(25))
@pytest.mark.parametrize("end", [(0.0, 0.0), None])
def test_best_route_matches_brute_force(cand, seed, end):
    from brain.planner import best_route, route_length

    rng = random.Random(seed)
    n = rng.randint(2, 7)
    targets = [
        cand(f"c_{i:04d}", x=rng.uniform(-1.5, 1.5), y=rng.uniform(-1.5, 1.5)) for i in range(n)
    ]
    start = (rng.uniform(-1, 1), rng.uniform(-1, 1))
    got = best_route(start, targets, end)
    assert sorted(c.cand_id for c in got) == sorted(c.cand_id for c in targets)
    assert route_length(start, got, end) == pytest.approx(_brute(start, targets, end))


def test_best_route_beats_nearest_neighbour_and_ends_home(cand):
    from brain.planner import best_route, route_length

    # Greedy NN from the origin grabs the close target first, then has to come all the way
    # back; the optimal round trip goes out the far side first and sweeps home.
    targets = [
        cand("c_0001", x=0.3, y=0.0),
        cand("c_0002", x=-1.0, y=0.0),
        cand("c_0003", x=-1.2, y=0.4),
    ]
    start, home = (0.0, 0.0), (0.0, 0.0)
    nn = nn_route(start, targets)
    best = best_route(start, targets, home)
    assert route_length(start, best, home) < route_length(start, nn, home)
    assert [c.cand_id for c in nn][0] == "c_0001"


def test_best_route_edge_cases(cand):
    from brain.planner import MAX_EXACT, best_route

    assert best_route((0, 0), []) == []
    one = [cand("c_0001", x=1, y=1)]
    assert best_route((0, 0), one) == one
    many = [cand(f"c_{i:04d}", x=i * 0.1, y=0.0) for i in range(MAX_EXACT + 1)]
    assert [c.cand_id for c in best_route((0, 0), many)] == [
        c.cand_id for c in nn_route((0, 0), many)
    ]
    a = best_route((0, 0), [cand("c_0002", x=1, y=0), cand("c_0001", x=-1, y=0)], (0.0, 0.0))
    b = best_route((0, 0), [cand("c_0001", x=-1, y=0), cand("c_0002", x=1, y=0)], (0.0, 0.0))
    assert [c.cand_id for c in a] == [c.cand_id for c in b]  # deterministic on ties
