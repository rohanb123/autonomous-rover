"""Heads bundle loading and prediction shapes, with tiny synthetic heads (no real data needed)."""

from __future__ import annotations

import joblib
import numpy as np
import pytest
from sklearn.linear_model import LogisticRegression

from ..heads import CATEGORIES, FORMAT, TERRAINS, Heads, HeadsMismatch


def _bundle(n_cat: int = 6) -> dict:
    rng = np.random.default_rng(0)
    x = rng.normal(size=(240, 8)).astype(np.float32)
    vis_y = (x[:, 0] > 0).astype(int)
    cat_y = np.arange(240) % n_cat
    x[:, 1] += cat_y  # make categories separable along one axis
    ter_y = np.arange(240) % 4
    x[:, 2] += 3 * ter_y
    return {
        "format": FORMAT,
        "version": "test+lr-v1",
        "embed_model": "Fake/w",
        "visual": LogisticRegression().fit(x, vis_y),
        "category": LogisticRegression(max_iter=2000).fit(x, cat_y),
        "terrain": LogisticRegression(max_iter=2000).fit(x, ter_y),
        "category_names": list(CATEGORIES),
        "terrain_names": list(TERRAINS),
    }


@pytest.fixture()
def heads_file(tmp_path):
    path = tmp_path / "heads.joblib"
    joblib.dump(_bundle(), path)
    return path


def test_predictions_have_the_score_contract_shapes(heads_file):
    h = Heads.load(heads_file, expect_model="Fake/w")
    x = np.random.default_rng(1).normal(size=(5, 8)).astype(np.float32)
    vis = h.visual(x)
    assert vis.shape == (5,) and np.all((vis >= 0) & (vis <= 1))
    probs = h.category_probs(x)
    assert len(probs) == 5 and all(set(p) == set(CATEGORIES) for p in probs)
    assert all(abs(sum(p.values()) - 1) < 1e-6 for p in probs)
    ter = h.terrain(x)
    assert len(ter) == 5 and set(ter) <= set(TERRAINS)
    assert h.name == "test+lr-v1"


def test_single_vector_is_accepted(heads_file):
    h = Heads.load(heads_file)
    assert h.visual(np.zeros(8)).shape == (1,)


def test_model_mismatch_is_refused(heads_file):
    with pytest.raises(HeadsMismatch):
        Heads.load(heads_file, expect_model="SomethingElse/x")


def test_bad_bundles_are_refused(tmp_path):
    b = _bundle()
    b["format"] = 99
    with pytest.raises(ValueError):
        Heads(b)
    b = _bundle()
    b["category_names"] = ["a", "b", "c", "d", "e", "f"]
    with pytest.raises(ValueError):
        Heads(b)


def test_category_head_missing_a_class_reports_zero(tmp_path):
    b = _bundle()
    x = np.random.default_rng(2).normal(size=(50, 8)).astype(np.float32)
    b["category"] = LogisticRegression().fit(x, np.arange(50) % 5)  # never saw class 5 (pebble)
    probs = Heads(b).category_probs(x[:2])
    assert all(p["pebble"] == 0.0 for p in probs)
    assert all(abs(sum(p.values()) - 1) < 1e-6 for p in probs)
