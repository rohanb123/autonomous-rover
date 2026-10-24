"""Merge repeated sightings of the same rock into one candidate (doc 00 §8)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from brain import settings
from marsrover_shared.schemas import Candidate


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def merge_or_new(
    prop: Mapping,
    cands: Mapping[str, Candidate],
    embeddings: Mapping[str, Sequence[float]],
) -> str | None:
    """Existing cand_id this proposal merges into, or None for a new candidate.

    Merge if within merge_xy_m AND cos(emb) ≥ merge_cos_min, or within merge_xy_always_m
    regardless. With several matches, the nearest wins. `prop` needs `xy` and `embedding`.
    Candidates already merged into another are skipped.
    """
    d = settings.load().dedupe
    px, py = prop["xy"]
    best: tuple[float, str] | None = None
    for cand_id, c in cands.items():
        if c.status == "merged":
            continue
        dist = math.hypot(c.x - px, c.y - py)
        if dist > d.merge_xy_m:
            continue
        emb = embeddings.get(cand_id)
        same = dist <= d.merge_xy_always_m or (
            emb is not None and cosine(prop["embedding"], emb) >= d.merge_cos_min
        )
        if same and (best is None or dist < best[0]):
            best = (dist, cand_id)
    return best[1] if best else None


def merge(cand: Candidate, prop_id: str, new: Candidate) -> Candidate:
    """Fold a new sighting into `cand`: append prop_id, keep the max-score fields."""
    prop_ids = cand.prop_ids + [p for p in [prop_id] if p not in cand.prop_ids]
    keep = new if new.score > cand.score else cand
    return keep.model_copy(
        update={
            "cand_id": cand.cand_id,
            "prop_ids": prop_ids,
            "status": cand.status,
            "rank": cand.rank,
        }
    )
