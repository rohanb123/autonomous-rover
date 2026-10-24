"""Brain settings: run constants from config/run.yaml (doc 00 §8 mirror), service URLs and
secrets from the environment (config/.env). Loaded once, read-only."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
RUN_YAML = ROOT / "config" / "run.yaml"
ENV_FILE = ROOT / "config" / ".env"


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
class Voice:
    asr_model: str
    asr_timeout_s: float
    asr_keyterms: tuple[str, ...]
    asr_language: str
    asr_whisper_size: str
    nlu_base_url: str
    nlu_model: str
    nlu_timeout_s: float


@dataclass(frozen=True)
class Settings:
    scoring: Scoring
    dedupe: Dedupe
    visit_radius_m: float
    extra_stations_if_radius_gt_m: float
    voice: Voice
    mission_defaults: dict


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
        voice=Voice(
            asr_model=raw["voice"]["asr_model"],
            asr_timeout_s=raw["voice"]["asr_timeout_s"],
            asr_keyterms=tuple(raw["voice"]["asr_keyterms"]),
            asr_language=raw["voice"]["asr_language"],
            asr_whisper_size=raw["voice"]["asr_faster_whisper_size"],
            nlu_base_url=raw["voice"]["nlu_base_url"],
            nlu_model=raw["voice"]["nlu_model"],
            nlu_timeout_s=raw["voice"]["nlu_timeout_s"],
        ),
        mission_defaults=dict(raw["mission_defaults"]),
    )


def load_env(path: Path = ENV_FILE) -> None:
    """Fill os.environ from config/.env without overriding what's already set."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def env(key: str, default: str = "") -> str:
    load_env()
    return os.environ.get(key, default)
