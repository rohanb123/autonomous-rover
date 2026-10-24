"""Scoring heads on top of frozen embeddings (docs/03 §4, docs/roles/P3 §4).

`ml/models/heads.joblib` (written by ml/train_heads.py) holds three scikit-learn logistic
regressions plus the name of the embedding model they were trained on:
  visual    P(science-interesting)                  -> /score `visual`
  category  float_rock layered veined sand bedrock pebble -> /score `category_probs`
  terrain   soil bedrock sand big_rock               -> /score `terrain`

The terrain head needs the AI4Mars crops. When they weren't available at training time the
bundle stores `terrain: None` and terrain is *derived* from the category head instead
(CATEGORY_TO_TERRAIN below); the heads version then ends in "+derived-terrain".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

REPO = Path(__file__).resolve().parents[2]
DEFAULT_PATH = REPO / "ml" / "models" / "heads.joblib"
CATEGORIES = ("float_rock", "layered", "veined", "sand", "bedrock", "pebble")
TERRAINS = ("soil", "bedrock", "sand", "big_rock")
FORMAT = 1
# Fallback when no AI4Mars terrain head was trained: most likely category -> terrain.
CATEGORY_TO_TERRAIN = {
    "sand": "sand",
    "bedrock": "bedrock",
    "layered": "bedrock",
    "veined": "bedrock",
    "pebble": "soil",
    "float_rock": "big_rock",
}


class HeadsMismatch(ValueError):
    """heads.joblib was trained on a different embedding model than the one in use."""


class Heads:
    def __init__(self, bundle: dict[str, Any]):
        if bundle.get("format") != FORMAT:
            raise ValueError(f"heads bundle format {bundle.get('format')!r}, expected {FORMAT}")
        self.embed_model: str = bundle["embed_model"]
        self.version: str = bundle.get("version", "")
        self._visual = bundle["visual"]
        self._category = bundle["category"]
        self._terrain = bundle["terrain"]
        self.category_names: list[str] = list(bundle["category_names"])
        self.terrain_names: list[str] = list(bundle["terrain_names"])
        missing = set(CATEGORIES) - set(self.category_names)
        if missing:
            raise ValueError(f"category head is missing {sorted(missing)}")

    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH, expect_model: str | None = None) -> Heads:
        import joblib

        heads = cls(joblib.load(path))
        if expect_model is not None and heads.embed_model != expect_model:
            raise HeadsMismatch(
                f"{path} was trained on {heads.embed_model}, but the embedder is {expect_model}; "
                "re-run ml/build_embeddings.py and ml/train_heads.py"
            )
        return heads

    @property
    def name(self) -> str:
        return self.version or "heads"

    @property
    def terrain_derived(self) -> bool:
        """True when terrain comes from the category head (no AI4Mars terrain head)."""
        return self._terrain is None

    def visual(self, emb: np.ndarray) -> np.ndarray:
        """(n,) P(science-interesting)."""
        p = self._visual.predict_proba(_as2d(emb))
        return p[:, list(self._visual.classes_).index(1)].astype(np.float64)

    def category_probs(self, emb: np.ndarray) -> list[dict[str, float]]:
        p = self._category.predict_proba(_as2d(emb))
        order = [self.category_names[int(c)] for c in self._category.classes_]
        return [
            {name: float(row[order.index(name)]) if name in order else 0.0 for name in CATEGORIES}
            for row in p
        ]

    def terrain(self, emb: np.ndarray) -> list[str]:
        if self._terrain is None:
            return [CATEGORY_TO_TERRAIN[max(p, key=p.get)] for p in self.category_probs(_as2d(emb))]
        idx = self._terrain.predict(_as2d(emb))
        return [self.terrain_names[int(i)] for i in idx]


def _as2d(emb: np.ndarray) -> np.ndarray:
    x = np.asarray(emb, dtype=np.float32)
    return x[None, :] if x.ndim == 1 else x
