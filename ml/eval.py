"""Re-score the saved heads on the held-out test splits and print a report (docs/03 §4 "Eval").

Recomputes predictions from ml/models/heads.joblib and ml/data/embeddings/*.npz (so it checks
that the saved bundle reproduces metrics.json), then prints per-class F1 and confusion matrices.
The arena hold-out is added by ml/arena_fewshot.py once P1's labeled photos exist.

Usage:  python ml/eval.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import yaml
from sklearn.metrics import confusion_matrix, f1_score, roc_auc_score

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "gx10"))
from vision.heads import CATEGORIES, TERRAINS, Heads  # noqa: E402

EMB = ROOT / "data" / "embeddings"


def table(title: str, labels: list[str], y: np.ndarray, pred: np.ndarray) -> None:
    f1 = f1_score(y, pred, labels=range(len(labels)), average=None, zero_division=0)
    cm = confusion_matrix(y, pred, labels=range(len(labels)))
    print(f"\n{title} (rows = true, cols = predicted)")
    print("              " + " ".join(f"{name[:8]:>8s}" for name in labels) + "      F1")
    for name, row, f in zip(labels, cm, f1, strict=True):
        print(f"  {name:11s} " + " ".join(f"{v:8d}" for v in row) + f"   {f:5.3f}")


def main() -> None:
    cmap = yaml.safe_load((ROOT / "class_map.yaml").read_text(encoding="utf-8"))
    msl, crops = np.load(EMB / "msl_v21.npz"), np.load(EMB / "ai4mars_crops.npz")
    model = json.loads(str(msl["meta"]))["model"]
    heads = Heads.load(ROOT / "models" / "heads.joblib", expect_model=model)
    print(f"heads {heads.name} on {model}")

    test = msl["split"] == "test"
    pos, neg = cmap["visual"]["positive"], cmap["visual"]["negative"]
    m = test & np.isin(msl["label"], pos + neg)
    y = np.isin(msl["label"][m], pos).astype(int)
    auc = roc_auc_score(y, heads.visual(msl["emb"][m]))
    print(f"\nvisual AUC on MSL test: {auc:.4f} (n={m.sum()})")

    to_cat = {lab: i for i, c in enumerate(CATEGORIES) for lab in cmap["category"][c]}
    m = test & np.isin(msl["label"], list(to_cat))
    y = np.array([to_cat[int(v)] for v in msl["label"][m]])
    probs = heads.category_probs(msl["emb"][m])
    pred = np.array([CATEGORIES.index(max(p, key=p.get)) for p in probs])
    table("category, MSL test", list(CATEGORIES), y, pred)

    m = crops["split"] == "test"
    y = crops["label"][m].astype(int)
    pred = np.array([TERRAINS.index(t) for t in heads.terrain(crops["emb"][m])])
    table("terrain, AI4Mars test crops", list(TERRAINS), y, pred)


if __name__ == "__main__":
    main()
