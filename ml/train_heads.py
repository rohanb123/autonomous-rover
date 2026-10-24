"""Train the visual / category / terrain heads on frozen embeddings (docs/03 §4-§5).

Inputs:  ml/data/embeddings/msl_v21.npz, ai4mars_crops.npz  (ml/build_embeddings.py)
         ml/class_map.yaml
Outputs: ml/models/heads.joblib   (loaded by gx10/vision/heads.py)
         ml/models/metrics.json   (docs/03 §5 acceptance: visual AUC >= 0.80 on MSL test)

Each head is a logistic regression with balanced class weights. The regularisation strength C
is picked on validation data (MSL val split; a stratified 10% of the AI4Mars train crops), then
the head is refit on the training split and scored once on the held-out test split.

Usage:  python ml/train_heads.py [--emb-dir DIR] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import joblib
import numpy as np
import yaml
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, roc_auc_score
from sklearn.model_selection import train_test_split

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "gx10"))
from vision.heads import CATEGORIES, FORMAT, TERRAINS  # noqa: E402

EMB_DIR = ROOT / "data" / "embeddings"
OUT_DIR = ROOT / "models"
C_GRID = (0.1, 0.3, 1.0, 3.0, 10.0)
SEED = 0


def load_npz(path: Path) -> dict:
    z = np.load(path)
    out = {k: z[k] for k in z.files if k != "meta"}
    out["emb"] = out["emb"].astype(np.float32)
    out["meta"] = json.loads(str(z["meta"]))
    return out


def fit(x: np.ndarray, y: np.ndarray, c: float) -> LogisticRegression:
    clf = LogisticRegression(C=c, class_weight="balanced", max_iter=5000)
    return clf.fit(x, y)


def pick_c(xtr, ytr, xva, yva, score) -> tuple[float, dict[str, float]]:
    scores = {str(c): float(score(fit(xtr, ytr, c), xva, yva)) for c in C_GRID}
    best = max(scores, key=scores.get)
    return float(best), scores


def macro_f1(model, x, y) -> float:
    return f1_score(y, model.predict(x), average="macro", zero_division=0)


def per_class(y_true, y_pred, names: list[str]) -> dict:
    labels = list(range(len(names)))
    f1 = f1_score(y_true, y_pred, labels=labels, average=None, zero_division=0)
    support = np.bincount(y_true, minlength=len(names))
    return {
        n: {"f1": round(float(f), 4), "support": int(s)}
        for n, f, s in zip(names, f1, support, strict=True)
    }


# --- heads ---------------------------------------------------------------------------------


def train_visual(msl: dict, cmap: dict) -> tuple[LogisticRegression, dict]:
    pos, neg = set(cmap["visual"]["positive"]), set(cmap["visual"]["negative"])
    keep = np.isin(msl["label"], list(pos | neg))
    y_all = np.isin(msl["label"], list(pos)).astype(int)

    def part(split):
        m = keep & (msl["split"] == split)
        return msl["emb"][m], y_all[m]

    (xtr, ytr), (xva, yva), (xte, yte) = part("train"), part("val"), part("test")
    c, grid = pick_c(xtr, ytr, xva, yva, lambda m, x, y: roc_auc_score(y, m.predict_proba(x)[:, 1]))
    clf = fit(xtr, ytr, c)
    p = clf.predict_proba(xte)[:, 1]
    auc = float(roc_auc_score(yte, p))
    return clf, {
        "test_auc": round(auc, 4),
        "test_accuracy_at_0.5": round(float(accuracy_score(yte, p >= 0.5)), 4),
        "meets_docs03_target_0.80": auc >= 0.80,
        "n_train": int(len(ytr)),
        "n_test": int(len(yte)),
        "test_positives": int(yte.sum()),
        "C": c,
        "val_auc_by_C": grid,
    }


def train_category(msl: dict, cmap: dict) -> tuple[LogisticRegression, dict]:
    to_cat = {lab: i for i, cat in enumerate(CATEGORIES) for lab in cmap["category"][cat]}
    keep = np.isin(msl["label"], list(to_cat))
    y_all = np.array([to_cat.get(int(lab), -1) for lab in msl["label"]])

    def part(split):
        m = keep & (msl["split"] == split)
        return msl["emb"][m], y_all[m]

    (xtr, ytr), (xva, yva), (xte, yte) = part("train"), part("val"), part("test")
    c, grid = pick_c(xtr, ytr, xva, yva, macro_f1)
    clf = fit(xtr, ytr, c)
    pred = clf.predict(xte)
    return clf, {
        "test_macro_f1": round(float(f1_score(yte, pred, average="macro", zero_division=0)), 4),
        "test_accuracy": round(float(accuracy_score(yte, pred)), 4),
        "per_class": per_class(yte, pred, list(CATEGORIES)),
        "confusion_matrix": confusion_matrix(yte, pred, labels=range(len(CATEGORIES))).tolist(),
        "labels": list(CATEGORIES),
        "n_train": int(len(ytr)),
        "n_test": int(len(yte)),
        "C": c,
        "val_macro_f1_by_C": grid,
        "note": "MSL only; bedrock<-Nearby surface and pebble<-Close-up rock are weak (docs/03)",
    }


def train_terrain(crops: dict) -> tuple[LogisticRegression, dict]:
    tr, te = crops["split"] == "train", crops["split"] == "test"
    x, y = crops["emb"][tr], crops["label"][tr].astype(int)
    xtr, xva, ytr, yva = train_test_split(x, y, test_size=0.1, stratify=y, random_state=SEED)
    c, grid = pick_c(xtr, ytr, xva, yva, macro_f1)
    clf = fit(x, y, c)  # refit on all train crops
    xte, yte = crops["emb"][te], crops["label"][te].astype(int)
    pred = clf.predict(xte)
    present = sorted(set(yte.tolist()))
    return clf, {
        "test_accuracy": round(float(accuracy_score(yte, pred)), 4),
        "test_macro_f1_present_classes": round(
            float(f1_score(yte, pred, labels=present, average="macro", zero_division=0)), 4
        ),
        "per_class": per_class(yte, pred, list(TERRAINS)),
        "confusion_matrix": confusion_matrix(yte, pred, labels=range(len(TERRAINS))).tolist(),
        "labels": list(TERRAINS),
        "train_counts": dict(zip(TERRAINS, np.bincount(y, minlength=4).tolist(), strict=True)),
        "n_test": int(len(yte)),
        "C": c,
        "val_macro_f1_by_C": grid,
        "note": "big_rock has few train crops and none in the AI4Mars test split",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Train heads on frozen embeddings (docs/03 §4).")
    ap.add_argument("--emb-dir", type=Path, default=EMB_DIR)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    t0 = time.time()
    cmap = yaml.safe_load((ROOT / "class_map.yaml").read_text(encoding="utf-8"))
    msl = load_npz(args.emb_dir / "msl_v21.npz")
    crops = load_npz(args.emb_dir / "ai4mars_crops.npz")
    if msl["meta"]["model"] != crops["meta"]["model"]:
        raise SystemExit(
            f"embedding models differ: {msl['meta']['model']} vs {crops['meta']['model']}"
        )
    model = msl["meta"]["model"]

    visual, m_vis = train_visual(msl, cmap)
    category, m_cat = train_category(msl, cmap)
    terrain, m_ter = train_terrain(crops)
    version = f"{model.split('/')[0].lower()}+lr-v1"
    bundle = {
        "format": FORMAT,
        "version": version,
        "embed_model": model,
        "visual": visual,
        "category": category,
        "terrain": terrain,
        "category_names": list(CATEGORIES),
        "terrain_names": list(TERRAINS),
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    metrics = {
        "version": version,
        "embed_model": model,
        "trained_at": bundle["trained_at"],
        "visual": m_vis,
        "category": m_cat,
        "terrain": m_ter,
    }
    args.out.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, args.out / "heads.joblib")
    (args.out / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"visual   AUC {m_vis['test_auc']:.3f} (target 0.80)  C={m_vis['C']}")
    print(f"category macro-F1 {m_cat['test_macro_f1']:.3f}  accuracy {m_cat['test_accuracy']:.3f}")
    for name, v in m_cat["per_class"].items():
        print(f"           {name:10s} F1 {v['f1']:.3f}  (n={v['support']})")
    print(
        f"terrain  accuracy {m_ter['test_accuracy']:.3f}  "
        f"macro-F1 {m_ter['test_macro_f1_present_classes']:.3f}"
    )
    for name, v in m_ter["per_class"].items():
        print(f"           {name:10s} F1 {v['f1']:.3f}  (n={v['support']})")
    print(
        f"wrote {args.out / 'heads.joblib'} and metrics.json ({version}) in {time.time() - t0:.0f}s"
    )


if __name__ == "__main__":
    main()
