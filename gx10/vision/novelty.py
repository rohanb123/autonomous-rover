"""Per-run novelty memory (docs/roles/P3 §4-§5, §10).

novelty = 1 − max cosine similarity to every earlier item of the same run (first item = 1.0),
filled in the order items arrive. One memory per run_id, so a new run starts fresh (a new
run_id is how a start_run reaches vision). Embeddings are L2-normalized, so cosine = dot product.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

import numpy as np


class NoveltyMemory:
    def __init__(self, max_items: int = 5000):
        self._mem: np.ndarray | None = None
        self._max = max_items

    def __len__(self) -> int:
        return 0 if self._mem is None else len(self._mem)

    def score_and_add(self, emb: np.ndarray) -> np.ndarray:
        """(n,) novelty in [0, 1] for each row of emb (or a single vector), then remember them."""
        x = np.atleast_2d(np.asarray(emb, dtype=np.float32))
        out = np.empty(len(x), dtype=np.float64)
        for i, v in enumerate(x):
            if self._mem is None or len(self._mem) == 0:
                out[i] = 1.0
                self._mem = v[None, :].copy()
            else:
                sim = float(np.max(self._mem @ v))
                out[i] = min(1.0, max(0.0, 1.0 - sim))
                self._mem = np.vstack([self._mem, v[None, :]])[-self._max :]
        return out


class NoveltyStore:
    """NoveltyMemory per run_id; keeps the most recently used `max_runs` runs."""

    def __init__(self, max_runs: int = 8):
        self._runs: OrderedDict[str, NoveltyMemory] = OrderedDict()
        self._max_runs = max_runs
        self._lock = threading.Lock()

    def score_and_add(self, run_id: str, emb: np.ndarray) -> np.ndarray:
        with self._lock:
            mem = self._runs.pop(run_id, None) or NoveltyMemory()
            self._runs[run_id] = mem
            while len(self._runs) > self._max_runs:
                self._runs.popitem(last=False)
            return mem.score_and_add(emb)

    def size(self, run_id: str) -> int:
        with self._lock:
            mem = self._runs.get(run_id)
            return 0 if mem is None else len(mem)
