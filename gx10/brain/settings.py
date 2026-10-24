"""Run constants from config/run.yaml (doc 00 §8 mirror). Loaded once, read-only."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

RUN_YAML = Path(__file__).resolve().parents[2] / "config" / "run.yaml"


@dataclass(frozen=True)
class Scoring:
    w_novelty: float
    w_visual: float
    w_pickable: float
    w_category: float
    w_dist: float  # applied as a penalty: score -= w_dist · dist_norm
    want_unrequested: float
    reason_max_chars: int


@dataclass(frozen=True)
class Dedupe:
    merge_xy_m: float
    merge_cos_min: float
    merge_xy_always_m: float


@dataclass(frozen=True)
class Settings:
    scoring: Scoring
    dedupe: Dedupe
    visit_radius_m: float
    extra_stations_if_radius_gt_m: float


@cache
def load(path: Path = RUN_YAML) -> Settings:
    raw = yaml.safe_load(path.read_text())
    w = raw["scoring"]["weights"]
    return Settings(
        scoring=Scoring(
            w_novelty=w["novelty"],
            w_visual=w["visual"],
            w_pickable=w["pickable"],
            w_category=w["category_score"],
            w_dist=-w["dist_norm"],  # stored negative in run.yaml
            want_unrequested=raw["scoring"]["want_unrequested"],
            reason_max_chars=raw["scoring"]["reason_max_chars"],
        ),
        dedupe=Dedupe(**raw["dedupe"]),
        visit_radius_m=raw["exclusion"]["visit_radius_m"],
        extra_stations_if_radius_gt_m=raw["survey"]["extra_stations_if_radius_gt_m"],
    )
