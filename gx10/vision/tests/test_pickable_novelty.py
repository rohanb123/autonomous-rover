from __future__ import annotations

import math

import numpy as np
import pytest

from ..novelty import NoveltyMemory, NoveltyStore
from ..pickable import pickable


@pytest.mark.parametrize(
    "size, expected",
    [
        (0.012, 1.0), (0.02, 1.0), (0.030, 1.0),          # full band (docs/roles/P3 §4)
        (0.005, 0.0), (0.045, 0.0), (0.0, 0.0), (0.2, 0.0),
        (0.0085, 0.5), (0.0375, 0.5),                     # halfway down each ramp
        (float("nan"), 0.0), (-0.01, 0.0),
    ],
)  # fmt: skip
def test_pickable_band_and_ramps(size, expected):
    assert math.isclose(pickable(size), expected, abs_tol=1e-9)


def _unit(*v):
    a = np.array(v, dtype=np.float32)
    return a / np.linalg.norm(a)


def test_first_item_is_fully_novel_then_repeats_are_not():
    mem = NoveltyMemory()
    a, b = _unit(1, 0, 0), _unit(0, 1, 0)
    assert mem.score_and_add(a)[0] == 1.0
    assert mem.score_and_add(a)[0] == pytest.approx(0.0, abs=1e-6)
    assert mem.score_and_add(b)[0] == pytest.approx(1.0, abs=1e-6)  # orthogonal
    assert len(mem) == 3


def test_batch_items_are_scored_in_arrival_order():
    mem = NoveltyMemory()
    a = _unit(1, 0, 0)
    out = mem.score_and_add(np.stack([a, a, _unit(1, 1, 0)]))
    assert out[0] == 1.0 and out[1] == pytest.approx(0.0, abs=1e-6)
    assert out[2] == pytest.approx(1 - math.cos(math.pi / 4), abs=1e-6)


def test_novelty_is_clamped_to_unit_interval():
    mem = NoveltyMemory()
    mem.score_and_add(_unit(1, 0))
    assert mem.score_and_add(_unit(-1, 0))[0] == 1.0  # cos = -1 -> clamp 2 to 1


def test_each_run_has_its_own_memory():
    store = NoveltyStore()
    a = _unit(1, 0, 0)
    assert store.score_and_add("r_1", a)[0] == 1.0
    assert store.score_and_add("r_1", a)[0] == pytest.approx(0.0, abs=1e-6)
    assert store.score_and_add("r_2", a)[0] == 1.0  # run 2 starts fresh (docs/roles/P3 §10)
    assert store.size("r_1") == 2 and store.size("r_2") == 1


def test_store_forgets_least_recent_runs():
    store = NoveltyStore(max_runs=2)
    for r in ("r_1", "r_2", "r_3"):
        store.score_and_add(r, _unit(1, 0))
    assert store.size("r_1") == 0 and store.size("r_3") == 1
