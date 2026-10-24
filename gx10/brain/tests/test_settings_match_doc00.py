"""config/run.yaml must mirror doc 00 §8 (P4 role doc: 'a test compares them')."""

import re
from pathlib import Path

from brain import settings

DOC00 = Path(__file__).resolve().parents[3] / "docs" / "00-overview-and-contracts.md"


def test_run_yaml_matches_doc00_section8():
    doc = DOC00.read_text(encoding="utf-8")
    m = re.search(
        r"score = ([\d.]+)·novelty \+ ([\d.]+)·visual \+ ([\d.]+)·pickable "
        r"\+ ([\d.]+)·category_score − ([\d.]+)·dist_norm",
        doc,
    )
    assert m, "doc 00 §8 score formula not found"
    s = settings.load()
    weights = (
        s.scoring.w_novelty,
        s.scoring.w_visual,
        s.scoring.w_pickable,
        s.scoring.w_category,
        s.scoring.w_dist,
    )
    assert weights == tuple(float(g) for g in m.groups())
    assert "else 0.2" in doc and s.scoring.want_unrequested == 0.2
    assert (
        f"‖xy‖ ≤ {s.dedupe.merge_xy_m} m" in doc and f"cos(emb) ≥ {s.dedupe.merge_cos_min}" in doc
    )
    assert f"‖xy‖ ≤ {s.dedupe.merge_xy_always_m} m" in doc
    assert f"visit_radius_m = {s.visit_radius_m:.2f}" in doc
    assert f"≤ {s.scoring.reason_max_chars} chars" in doc
