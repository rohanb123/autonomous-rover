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
