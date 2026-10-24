import pytest

from brain.dedupe import merge, merge_or_new
from brain.visited import excluded, newly_excluded
from marsrover_shared.schemas import Visit

E1, E2 = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]  # cos = 0 between them


@pytest.mark.parametrize(
    "xy, emb, want",
    [
        ((0.05, 0.0), E2, "c_0001"),  # ≤ 0.06 m merges regardless of looks
        ((0.12, 0.0), E1, "c_0001"),  # ≤ 0.15 m and same embedding
        ((0.12, 0.0), E2, None),  # ≤ 0.15 m but looks different
        ((0.20, 0.0), E1, None),  # too far
    ],
)
def test_merge_rules(cand, xy, emb, want):
    cands = {"c_0001": cand("c_0001")}
    assert merge_or_new({"xy": xy, "embedding": emb}, cands, {"c_0001": E1}) == want


def test_merge_picks_nearest_and_skips_merged(cand):
    cands = {
        "c_0001": cand("c_0001", x=0.10),
        "c_0002": cand("c_0002", x=0.04),
        "c_0003": cand("c_0003", x=0.0, status="merged"),
    }
    embs = dict.fromkeys(cands, E1)
    assert merge_or_new({"xy": (0.0, 0.0), "embedding": E1}, cands, embs) == "c_0002"


def test_merge_keeps_max_score_and_appends_prop(cand):
    old = cand("c_0001", score=0.4, status="queued", rank=2, prop_ids=["p_0001"])
    better = cand("c_0099", score=0.7, prop_ids=["p_0007"], reason="better view")
    m = merge(old, "p_0007", better)
    assert (m.cand_id, m.score, m.reason, m.status, m.rank) == (
        "c_0001",
        0.7,
        "better view",
        "queued",
        2,
    )
    assert m.prop_ids == ["p_0001", "p_0007"]
    assert merge(old, "p_0008", cand("c_0098", score=0.1)).score == 0.4


def test_exclusion_radius(cand):
    visits = [
        Visit(
            visit_id="v_0001", run_id="r_1", cand_id="c_0001", x=1.0, y=0.0, t=0, outcome="sampled"
        )
    ]
    assert excluded((1.19, 0.0), visits) and not excluded((1.21, 0.0), visits)
    cands = {
        "c_0002": cand("c_0002", x=1.1),
        "c_0003": cand("c_0003", x=1.1, status="visited"),
        "c_0004": cand("c_0004", x=2.0, status="queued"),
    }
    assert newly_excluded(cands, visits) == ["c_0002"]
