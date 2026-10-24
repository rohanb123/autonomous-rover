"""Tests for the SIMULATED Bradford assay (docs/roles/P3-perception-ai.md §7 test table)."""

from __future__ import annotations

import json
from collections import Counter
from statistics import mean

from marsrover_shared.schemas import Assay

from ..bradford_sim import LINEAR_RANGE, seed_for, simulate, verdict_for

GOLDEN_RUN = "r_20261024_101500"
RUN = "r_20261024_120000"


def _ids(n: int) -> list[str]:
    return [f"a_{i:04d}" for i in range(1, n + 1)]


def test_golden_vector():
    out = simulate("a_0001", GOLDEN_RUN, 0.63)
    fit = out["standard_curve_params"]["fit"]
    assert out["seed"] == 3896889563
    assert out["a595"] == 0.2585
    assert out["concentration"] == 282.1
    assert out["verdict"] == "low"
    assert out["in_linear_range"] is True
    assert (fit["slope"], fit["intercept"], fit["r2"]) == (0.0007188, 0.0557, 0.9947)
    assert out["standard_curve_params"]["blank_a595"] == -0.0068


def test_golden_standards_match_doc00():
    # The three standards doc 00 §6.7 prints in full.
    stds = simulate("a_0001", GOLDEN_RUN, 0.63)["standard_curve_params"]["standards"]
    assert stds[:3] == [
        {"c": 0, "a595": [0.0073, -0.0063, -0.001]},
        {"c": 25, "a595": [0.0368, 0.0404, 0.0364]},
        {"c": 125, "a595": [0.1281, 0.1487, 0.134]},
    ]
    assert [s["c"] for s in stds] == [0, 25, 125, 250, 500, 750, 1000, 1500, 2000]
    assert all(len(s["a595"]) == 3 for s in stds)


def test_determinism():
    assert simulate("a_0007", RUN, 0.4) == simulate("a_0007", RUN, 0.4)


def test_different_samples_get_different_seeds():
    assert seed_for("a_0001", RUN) != seed_for("a_0002", RUN)


def test_fit_quality_over_200_samples():
    for sid in _ids(200):
        assert simulate(sid, RUN)["standard_curve_params"]["fit"]["r2"] >= 0.98


def test_monotonic_bias():
    lo = mean(simulate(sid, RUN, 0.1)["concentration"] for sid in _ids(300))
    hi = mean(simulate(sid, RUN, 0.9)["concentration"] for sid in _ids(300))
    assert hi > lo


def test_labels_always_simulated():
    for sid in _ids(50):
        for score in (None, 0.0, 0.5, 1.0):
            out = simulate(sid, RUN, score)
            assert out["simulated"] is True
            assert out["assay_type"] == "bradford_sim"
            assert out["units"] == "µg/mL"
            assert out["model_version"] == "bradford_sim-1"


def test_distribution_sanity_all_verdicts_appear():
    counts = Counter(
        simulate(sid, RUN, score)["verdict"] for sid in _ids(300) for score in (0.1, 0.5, 0.9)
    )
    assert sum(counts.values()) == 900
    assert set(counts) == {"below_detection", "low", "elevated"}


def test_json_safety():
    out = simulate("a_0001", GOLDEN_RUN, 0.63)
    assert 0 <= out["seed"] < 2**32
    round_trip = json.loads(json.dumps(out))
    # tuples become lists in JSON; the simulator already emits lists, so it round-trips exactly
    assert round_trip == out


def test_output_validates_against_shared_contract():
    for sid in _ids(20):
        for score in (None, 0.1, 0.9):
            Assay.model_validate(simulate(sid, RUN, score))


def test_verdict_bands_and_linear_range():
    assert verdict_for(124.9) == "below_detection"
    assert verdict_for(125.0) == "low"
    assert verdict_for(499.9) == "low"
    assert verdict_for(500.0) == "elevated"
    assert LINEAR_RANGE == (125.0, 1000.0)
