"""Embed the training images with the frozen open_clip model (docs/03 §4).

Outputs (ml/data/embeddings/, gitignored):
  msl_v21.npz        all 6,820 MSL v2.1 images: emb, label (0-18), split (train/val/test), file
  ai4mars_crops.npz  128×128 terrain crops: emb, label (0 soil, 1 bedrock, 2 sand, 3 big rock),
                     split (train from the train shards, test from test_min3), source image
Each file also stores `model` (open_clip name/pretrained) so heads can't be trained against
embeddings from a different model by mistake.

AI4Mars crops (docs/03 §4): per labeled image, sample 128×128 patches where one class covers
>= 80% of the patch (255 = unlabeled never counts), label = that class; up to 2 crops per image.
Sampling is seeded, so reruns give the same crops.

Usage:  python ml/build_embeddings.py [--only msl|ai4mars] [--ai4mars-images 4000]
Model:  config/models.yaml (embedding.open_clip_model / pretrained), or --model/--pretrained.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(REPO / "gx10"))
from vision.embed import Embedder, load_model_config  # noqa: E402

MSL_DIR = ROOT / "data" / "msl_v21" / "msl-labeled-data-set-v2.1"
AI4MARS_DIR = ROOT / "data" / "ai4mars" / "data"
OUT_DIR = ROOT / "data" / "embeddings"

CROP_PX = 128
CROP_PURITY = 0.80
UNLABELED = 255
TERRAIN_CLASSES = ("soil", "bedrock", "sand", "big_rock")


# --- MSL v2.1 ------------------------------------------------------------------------------


def msl_items(msl_dir: Path) -> list[tuple[str, int, str]]:
    items = []
    for split in ("train", "val", "test"):
        for line in (msl_dir / f"{split}-set-v2.1.txt").read_text(encoding="utf-8").splitlines():
            if line.strip():
                name, label = line.split()
                items.append((name, int(label), split))
    return items


def build_msl(emb: Embedder, msl_dir: Path, batch: int) -> dict:
    items = msl_items(msl_dir)
    vecs = []
    for i in range(0, len(items), 512):
        chunk = items[i : i + 512]
        imgs = [Image.open(msl_dir / "images" / name) for name, _, _ in chunk]
        vecs.append(emb.embed(imgs, batch_size=batch))
        print(f"  msl {min(i + 512, len(items))}/{len(items)}", flush=True)
    return {
        "emb": np.concatenate(vecs).astype(np.float16),
        "label": np.array([lab for _, lab, _ in items], dtype=np.int16),
        "split": np.array([s for _, _, s in items]),
        "file": np.array([n for n, _, _ in items]),
    }


# --- AI4Mars crops ---------------------------------------------------------------------------


def sample_crops(
    mask: np.ndarray, rng: np.random.Generator, n: int = 2, tries: int = 40
) -> list[tuple[int, int, int]]:
    """Up to n (row, col, label) crops of CROP_PX where one class covers >= CROP_PURITY.

    Crops don't overlap. Labels other than 0-3 (e.g. 255) never count toward purity.
    """
    h, w = mask.shape
    if h < CROP_PX or w < CROP_PX:
        return []
    picked: list[tuple[int, int, int]] = []
    for _ in range(tries):
        r = int(rng.integers(0, h - CROP_PX + 1))
        c = int(rng.integers(0, w - CROP_PX + 1))
        if any(abs(r - pr) < CROP_PX and abs(c - pc) < CROP_PX for pr, pc, _ in picked):
            continue
        patch = mask[r : r + CROP_PX, c : c + CROP_PX]
        counts = np.bincount(patch.ravel(), minlength=256)[: len(TERRAIN_CLASSES)]
        label = int(counts.argmax())
        if counts[label] >= CROP_PURITY * patch.size:
            picked.append((r, c, label))
            if len(picked) == n:
                break
    return picked


def ai4mars_rows(shards: list[Path], limit: int | None):
    """Yield (source name, grayscale image, label mask) for labeled rows."""
    import pyarrow.parquet as pq

    seen = 0
    for shard in shards:
        pf = pq.ParquetFile(shard)
        for g in range(pf.num_row_groups):
            for row in pf.read_row_group(
                g, columns=["image", "label_mask", "has_labels"]
            ).to_pylist():
                lm = row["label_mask"]
                if not row["has_labels"] or not lm or not lm.get("bytes"):
                    continue
                img = Image.open(io.BytesIO(row["image"]["bytes"]))
                mask = np.array(Image.open(io.BytesIO(lm["bytes"])))
                yield row["image"].get("path") or f"{shard.name}:{seen}", img, mask
                seen += 1
                if limit is not None and seen >= limit:
                    return


def build_ai4mars(
    emb: Embedder, data_dir: Path, n_images: int, per_image: int, batch: int, seed: int
) -> dict:
    rng = np.random.default_rng(seed)
    parts = {"emb": [], "label": [], "split": [], "source": []}
    plans = [
        ("train", sorted(data_dir.glob("train-*.parquet")), n_images),
        ("test", sorted(data_dir.glob("test_min3-*.parquet")), None),
    ]
    for split, shards, limit in plans:
        if not shards:
            print(f"  ai4mars: no {split} shards in {data_dir}, skipping", flush=True)
            continue
        crops, labels, sources, n_img = [], [], [], 0
        for name, img, mask in ai4mars_rows(shards, limit):
            n_img += 1
            if mask.shape != (img.height, img.width):
                continue
            for r, c, label in sample_crops(mask, rng, per_image):
                crops.append(img.crop((c, r, c + CROP_PX, r + CROP_PX)).convert("RGB"))
                labels.append(label)
                sources.append(name)
            if len(crops) >= 1024:
                parts["emb"].append(emb.embed(crops, batch_size=batch))
                parts["label"] += labels
                parts["split"] += [split] * len(labels)
                parts["source"] += sources
                crops, labels, sources = [], [], []
                print(f"  ai4mars {split}: {n_img} images, {len(parts['label'])} crops", flush=True)
        if crops:
            parts["emb"].append(emb.embed(crops, batch_size=batch))
            parts["label"] += labels
            parts["split"] += [split] * len(labels)
            parts["source"] += sources
        print(f"  ai4mars {split}: {n_img} labeled images used", flush=True)
    if not parts["emb"]:
        raise SystemExit(f"no AI4Mars crops made from {data_dir}")
    return {
        "emb": np.concatenate(parts["emb"]).astype(np.float16),
        "label": np.array(parts["label"], dtype=np.int16),
        "split": np.array(parts["split"]),
        "source": np.array(parts["source"]),
    }


# --- main --------------------------------------------------------------------------------------


def save(path: Path, arrays: dict, emb: Embedder, extra: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"model": emb.name, "dim": int(arrays["emb"].shape[1]), **extra}
    np.savez_compressed(path, **arrays, meta=np.array(json.dumps(meta)))
    labels, counts = np.unique(arrays["label"], return_counts=True)
    splits, scounts = np.unique(arrays["split"], return_counts=True)
    split_counts = dict(zip(splits.tolist(), scounts.tolist(), strict=True))
    label_counts = dict(zip(labels.tolist(), counts.tolist(), strict=True))
    print(f"wrote {path.name}: {arrays['emb'].shape}, splits {split_counts}")
    print(f"  label counts {label_counts}", flush=True)


def main() -> None:
    cfg = load_model_config()["embedding"]
    ap = argparse.ArgumentParser(description="Embed MSL v2.1 and AI4Mars crops (docs/03 §4).")
    ap.add_argument("--model", default=cfg["open_clip_model"])
    ap.add_argument("--pretrained", default=cfg["pretrained"])
    ap.add_argument("--device", default=cfg.get("device", "cuda"))
    ap.add_argument("--only", choices=["msl", "ai4mars"])
    ap.add_argument("--ai4mars-images", type=int, default=4000, help="labeled train images")
    ap.add_argument("--crops-per-image", type=int, default=2)
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    t0 = time.time()
    emb = Embedder(args.model, args.pretrained, args.device)
    print(f"model {emb.name} on {emb.device} ({time.time() - t0:.0f}s to load)", flush=True)
    if args.only in (None, "msl"):
        save(args.out / "msl_v21.npz", build_msl(emb, MSL_DIR, args.batch), emb, {})
    if args.only in (None, "ai4mars"):
        arrays = build_ai4mars(
            emb, AI4MARS_DIR, args.ai4mars_images, args.crops_per_image, args.batch, args.seed
        )
        extra = {
            "classes": TERRAIN_CLASSES,
            "crop_px": CROP_PX,
            "purity": CROP_PURITY,
            "seed": args.seed,
        }
        save(args.out / "ai4mars_crops.npz", arrays, emb, extra)
    print(f"done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
