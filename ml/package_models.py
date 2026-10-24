"""Bundle the model files (and the open_clip weights) to move them to another machine (docs/03 §7).

pack:    zip ml/models/{heads.joblib, metrics.json, category_priors*.json} plus the embedding
         model's Hugging Face cache folder (SigLIP ViT-B/16 ≈ 780 MB) and a manifest with
         sha256 checksums. The zip is a local file; it never goes into git.
unpack:  check every checksum, put the model files in ml/models/ and the weights in the local
         Hugging Face cache (or --hf-cache). On a machine without internet, then run the
         services with HF_HUB_OFFLINE=1 so nothing tries to download.

Usage:  python ml/package_models.py pack   [--out FILE] [--models-dir DIR]
        python ml/package_models.py unpack FILE [--models-dir DIR] [--hf-cache DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

ML = Path(__file__).resolve().parent
REPO = ML.parent
sys.path.insert(0, str(REPO / "gx10"))
from vision.embed import load_model_config  # noqa: E402

MODEL_FILES = (
    "heads.joblib",
    "metrics.json",
    "category_priors.json",
    "category_priors_detail.json",
)
STORE_SUFFIXES = (".safetensors", ".bin", ".joblib")  # already dense: don't deflate
CHUNK = 1 << 20


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def hf_cache_dir() -> Path:
    from huggingface_hub import constants

    return Path(constants.HF_HUB_CACHE)


def model_repo_folder() -> tuple[str, str]:
    """(open_clip name/pretrained, HF cache folder name) for the configured embedding model."""
    import open_clip

    emb = load_model_config()["embedding"]
    cfg = open_clip.get_pretrained_cfg(emb["open_clip_model"], emb["pretrained"])
    hub = (cfg or {}).get("hf_hub", "").strip("/")
    if not hub:
        raise SystemExit(f"{emb['open_clip_model']}/{emb['pretrained']} has no HF hub weights")
    return f"{emb['open_clip_model']}/{emb['pretrained']}", "models--" + hub.replace("/", "--")


def pack(out: Path, models_dir: Path) -> None:
    name, folder = model_repo_folder()
    weights = hf_cache_dir() / folder
    if not weights.is_dir():
        raise SystemExit(
            f"{weights} not found: run ml/setup_models.py once to download the weights"
        )
    entries: list[tuple[Path, str]] = []
    for f in MODEL_FILES:
        p = models_dir / f
        if p.exists():
            entries.append((p, f"models/{f}"))
        elif f == "heads.joblib":
            raise SystemExit(f"{p} missing: run ml/setup_models.py first")
    for p in sorted(weights.rglob("*")):
        if p.is_file():
            entries.append((p, f"hf_cache/{folder}/{p.relative_to(weights).as_posix()}"))
    manifest = {
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "embed_model": name,
        "hf_cache_folder": folder,
        "files": {arc: {"sha256": sha256(p), "bytes": p.stat().st_size} for p, arc in entries},
    }
    metrics = models_dir / "metrics.json"
    if metrics.exists():
        manifest["heads_version"] = json.loads(metrics.read_text(encoding="utf-8")).get("version")
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", allowZip64=True) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for p, arc in entries:
            method = zipfile.ZIP_STORED if p.suffix in STORE_SUFFIXES else zipfile.ZIP_DEFLATED
            zf.write(p, arc, compress_type=method)
    total = sum(v["bytes"] for v in manifest["files"].values())
    print(f"packed {len(entries)} files ({total / 1e6:.0f} MB) for {name} -> {out}")


def _safe_target(base: Path, rel: str) -> Path:
    target = (base / rel).resolve()
    if not target.is_relative_to(base.resolve()):
        raise SystemExit(f"unsafe path in bundle: {rel!r}")
    return target


def unpack(bundle: Path, models_dir: Path, hf_cache: Path) -> None:
    with zipfile.ZipFile(bundle) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        for arc, info in manifest["files"].items():
            parts = PurePosixPath(arc).parts
            if parts[0] == "models":
                target = _safe_target(models_dir, "/".join(parts[1:]))
            elif parts[0] == "hf_cache":
                target = _safe_target(hf_cache, "/".join(parts[1:]))
            else:
                raise SystemExit(f"unexpected entry {arc!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha256()
            with zf.open(arc) as src, target.open("wb") as dst:
                for block in iter(lambda src=src: src.read(CHUNK), b""):
                    h.update(block)
                    dst.write(block)
            if h.hexdigest() != info["sha256"]:
                target.unlink()
                raise SystemExit(f"checksum mismatch for {arc} (deleted); the bundle is damaged")
    print(f"unpacked {len(manifest['files'])} files, checksums OK")
    print(f"  model files -> {models_dir}")
    print(f"  weights     -> {hf_cache / manifest['hf_cache_folder']}")
    print(f"  embedding model {manifest['embed_model']}, heads {manifest.get('heads_version')}")
    print("  offline machine: run the services with HF_HUB_OFFLINE=1")


def main() -> None:
    ap = argparse.ArgumentParser(description="Bundle model files and weights for another machine.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pack")
    p.add_argument("--out", type=Path, default=ML / "dist" / "marsrover-models.zip")
    p.add_argument("--models-dir", type=Path, default=ML / "models")
    u = sub.add_parser("unpack")
    u.add_argument("bundle", type=Path)
    u.add_argument("--models-dir", type=Path, default=ML / "models")
    u.add_argument("--hf-cache", type=Path, default=None, help="default: local HF hub cache")
    args = ap.parse_args()
    if args.cmd == "pack":
        pack(args.out, args.models_dir)
    else:
        unpack(args.bundle, args.models_dir, args.hf_cache or hf_cache_dir())


if __name__ == "__main__":
    main()
