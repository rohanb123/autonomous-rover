"""One command to create every model file the services need (docs/03 §7).

Runs, in order, skipping any step whose output already exists (use --force to redo):
  1. download MSL v2.1 (55 MB) and the ChemCam list (26 MB)
  2. download AI4Mars (6.5 GB)                       only with --with-ai4mars
  3. embed MSL (and AI4Mars crops)                  ml/build_embeddings.py, downloads the
                                                    SigLIP weights (~800 MB) on first run
  4. train the heads                                ml/train_heads.py -> heads.joblib, metrics.json
  5. build the ChemCam priors                       ml/chemcam_priors.py -> category_priors*.json
  6. print the evaluation report                    ml/eval.py

Without --with-ai4mars the terrain head is derived from the category head (fine for testing;
use --with-ai4mars for the real terrain head). Needs internet for steps 1-3 the first time;
for an offline machine, use a bundle from ml/package_models.py instead.

Usage:  python ml/setup_models.py [--with-ai4mars] [--force] [--data-dir DIR] [--models-dir DIR]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ML = Path(__file__).resolve().parent
PY = sys.executable


def run(step: str, args: list[str]) -> None:
    print(f"\n=== {step}: {' '.join(Path(a).name if a.endswith('.py') else a for a in args)}")
    t0 = time.time()
    subprocess.run([PY, *args], check=True)
    print(f"=== {step} done in {time.time() - t0:.0f}s")


def main() -> None:
    ap = argparse.ArgumentParser(description="Create the model files the services need.")
    ap.add_argument("--with-ai4mars", action="store_true", help="also download AI4Mars (6.5 GB)")
    ap.add_argument("--force", action="store_true", help="redo embeddings, heads and priors")
    ap.add_argument("--data-dir", type=Path, default=ML / "data")
    ap.add_argument("--models-dir", type=Path, default=ML / "models")
    ap.add_argument("--no-eval", action="store_true")
    args = ap.parse_args()

    data, models = args.data_dir, args.models_dir
    msl_dir = data / "msl_v21"
    msl_images = msl_dir / "msl-labeled-data-set-v2.1"
    chemcam_csv = data / "chemcam" / "msl_ccam_obs.csv"
    ai4mars_dir = data / "ai4mars"
    emb_dir = data / "embeddings"
    t0 = time.time()

    if not (msl_images / "images").is_dir():
        run("MSL v2.1", [str(ML / "download" / "get_msl_v21.py"), "--out", str(msl_dir)])
    else:
        print(f"skip MSL download ({msl_images} exists)")
    if not chemcam_csv.exists():
        run(
            "ChemCam",
            [str(ML / "download" / "get_chemcam_obs.py"), "--out", str(chemcam_csv.parent)],
        )
    else:
        print(f"skip ChemCam download ({chemcam_csv} exists)")
    if args.with_ai4mars:  # the script itself skips verified shards
        run("AI4Mars", [str(ML / "download" / "get_ai4mars.py"), "--out", str(ai4mars_dir)])

    common = ["--out", str(emb_dir), "--msl-dir", str(msl_images)]
    if args.force or not (emb_dir / "msl_v21.npz").exists():
        run("embed MSL", [str(ML / "build_embeddings.py"), "--only", "msl", *common])
    else:
        print("skip MSL embeddings (msl_v21.npz exists)")
    crops = emb_dir / "ai4mars_crops.npz"
    if args.with_ai4mars and (args.force or not crops.exists()):
        run(
            "embed AI4Mars",
            [str(ML / "build_embeddings.py"), "--only", "ai4mars", *common,
             "--ai4mars-dir", str(ai4mars_dir / "data")],
        )  # fmt: skip

    heads = models / "heads.joblib"
    if args.force or not heads.exists():
        train = [str(ML / "train_heads.py"), "--emb-dir", str(emb_dir), "--out", str(models)]
        if not args.with_ai4mars and not crops.exists():
            train.append("--no-ai4mars")
        run("train heads", train)
    else:
        print(f"skip training ({heads} exists; --force to retrain)")
    if args.force or not (models / "category_priors.json").exists():
        run(
            "ChemCam priors",
            [str(ML / "chemcam_priors.py"), "--csv", str(chemcam_csv), "--out", str(models)],
        )
    else:
        print("skip priors (category_priors.json exists)")
    if not args.no_eval:
        run(
            "eval",
            [str(ML / "eval.py"), "--emb-dir", str(emb_dir), "--models-dir", str(models)],
        )

    print(f"\nmodel files ready in {models} ({time.time() - t0:.0f}s):")
    for f in sorted(models.glob("*")):
        if f.is_file():
            print(f"  {f.name:32s} {f.stat().st_size / 1e6:7.2f} MB")


if __name__ == "__main__":
    main()
