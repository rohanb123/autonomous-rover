import pytest

from brain.scoring import category_score, explain, final_score
from marsrover_shared.schemas import Mission, Pose

MISSION = Mission(
    mission_id="m_0001", run_id="r_1", text="t", radius_m=1.5, k=3, categories=["float_rock"],
    sample=True, return_home=True, max_duration_s=900, mode="act",
)  # fmt: skip
PRIORS = {"float_rock": 1.0, "layered": 0.5}
ITEM = {
    "novelty": 0.64, "visual": 0.71, "pickable": 0.9, "xy": (1.12, 0.21), "size_m": 0.028,
    "category_probs": {"float_rock": 0.55, "layered": 0.2, "bedrock": 0.25},
}  # fmt: skip


def test_category_score_weights_unrequested_by_0_2():
    # 0.55·1.0·1 + 0.2·0.5·0.2 + 0.25·0(no prior) = 0.57
    assert category_score(ITEM["category_probs"], PRIORS, ["float_rock"]) == pytest.approx(0.57)
    # 'any' wants everything: 0.55 + 0.2·0.5 = 0.65
    assert category_score(ITEM["category_probs"], PRIORS, ["any"]) == pytest.approx(0.65)


def test_final_score_matches_hand_computation():
    score, comp = final_score(ITEM, MISSION, PRIORS, Pose(x=0, y=0, heading_deg=0, source="dr"))
    # 0.35·0.64 + 0.25·0.71 + 0.15·0.9 + 0.15·0.57 − 0.10·(1.13951/1.5)
    assert score == pytest.approx(0.224 + 0.1775 + 0.135 + 0.0855 - 0.0759674, abs=1e-6)
    assert comp["distance_m"] == pytest.approx(1.1395174)
    assert comp["size_m"] == 0.028


def test_distance_penalty_caps_at_radius():
    far = {**ITEM, "xy": (10.0, 0.0)}
    near = {**ITEM, "xy": (1.5, 0.0)}
    pose = Pose(x=0, y=0, heading_deg=0, source="dr")
    assert final_score(far, MISSION, PRIORS, pose)[0] == pytest.approx(
        final_score(near, MISSION, PRIORS, pose)[0]
    )


def test_explain_top_two_category_and_distance():
    _, comp = final_score(ITEM, MISSION, PRIORS, Pose(x=0, y=0, heading_deg=0, source="dr"))
    text = explain(comp, "float_rock", ["float_rock"], comp["distance_m"])
    assert text == (
        "Novel texture (0.64), visually interesting (0.71), "
        "matches requested 'float_rock'; 1.1 m away."
    )
    assert "not a requested" in explain(comp, "bedrock", ["float_rock"], 0.4)
    assert len(explain(comp, "x" * 200, ["float_rock"], 0.4)) == 140
