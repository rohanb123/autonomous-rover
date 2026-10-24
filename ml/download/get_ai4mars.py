"""Download the AI4Mars Hugging Face mirror (docs/03 §2: preferred download, ~6.47 GB parquet).

Source: https://huggingface.co/datasets/hassanjbara/AI4MARS (MSL subset of AI4Mars v0.1;
the card says CC0, Zenodo says CC-BY-4.0 -- attribute anyway, docs/03 §6 and U-19).
Each parquet shard has columns image, label_mask, rover_mask, range_mask, has_masks, has_labels.

- Files go to ml/data/ai4mars/data/*.parquet (gitignored). Nothing downloaded is executed.
- Resumable: partial files are kept as *.part and continued with an HTTP Range request.
- Every file is checked against the sha256 the Hub publishes; a mismatch is deleted.
- --max-train-shards N downloads only the first N of 13 train shards (each ~1,400 images),
  e.g. 3 shards ~ 4,200 images, enough for docs/03's 2 crops x 4,000 images.

Usage:  python ml/download/get_ai4mars.py [--max-train-shards N] [--no-test] [--out DIR]
Crops for the terrain head are cut later by ml/build_embeddings.py, not here.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

import httpx

REPO = "hassanjbara/AI4MARS"
API_TREE = f"https://huggingface.co/api/datasets/{REPO}/tree/main/data"
RESOLVE = f"https://huggingface.co/datasets/{REPO}/resolve/main/"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "ai4mars"
CHUNK = 1 << 20


def list_files(client: httpx.Client) -> list[dict]:
    resp = client.get(API_TREE)
    resp.raise_for_status()
    files = [f for f in resp.json() if f.get("type") == "file" and f["path"].endswith(".parquet")]
    return sorted(files, key=lambda f: f["path"])


def select(files: list[dict], max_train: int | None, test: bool) -> list[dict]:
    train = [f for f in files if "/train-" in f["path"]]
    tests = [f for f in files if "/test_" in f["path"]]
    if max_train is not None:
        train = train[:max_train]
    return train + (tests if test else [])


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def download(client: httpx.Client, f: dict, out_dir: Path) -> None:
    dest = out_dir / f["path"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    size, sha = f["size"], f.get("lfs", {}).get("oid")
    if dest.exists() and dest.stat().st_size == size:
        print(f"  ok (already here) {f['path']}", flush=True)
        return
    part = dest.with_name(dest.name + ".part")
    for attempt in range(5):
        have = part.stat().st_size if part.exists() else 0
        if have > size:
            part.unlink()
            have = 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with client.stream("GET", RESOLVE + f["path"], headers=headers) as resp:
                if resp.status_code == 200 and have:
                    have = 0  # server ignored Range: start over
                elif resp.status_code not in (200, 206):
                    resp.raise_for_status()
                mode = "ab" if have else "wb"
                t0, done, last = time.time(), have, 0.0
                with part.open(mode) as fh:
                    for chunk in resp.iter_bytes(CHUNK):
                        fh.write(chunk)
                        done += len(chunk)
                        if time.time() - last > 10:
                            rate = (done - have) / max(time.time() - t0, 1e-6) / 1e6
                            print(f"    {f['path']}: {done / size:5.1%} at {rate:.1f} MB/s",
                                  flush=True)  # fmt: skip
                            last = time.time()
            break
        except (httpx.HTTPError, OSError) as e:
            print(f"    retry {attempt + 1}/5 after error: {e}", flush=True)
            time.sleep(2 * (attempt + 1))
    else:
        raise SystemExit(f"giving up on {f['path']}")
    if part.stat().st_size != size:
        raise SystemExit(f"size mismatch for {f['path']}: {part.stat().st_size} != {size}")
    if sha and sha256_of(part) != sha:
        part.unlink()
        raise SystemExit(f"sha256 mismatch for {f['path']} (deleted; rerun to retry)")
    part.replace(dest)
    print(f"  ok {f['path']} ({size / 1e6:.0f} MB, sha256 verified)", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Download the AI4Mars HF mirror (parquet).")
    ap.add_argument("--max-train-shards", type=int, default=None, help="default: all 13")
    ap.add_argument("--no-test", action="store_true", help="skip the 3 small test splits")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    timeout = httpx.Timeout(30.0, read=120.0)
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        files = select(list_files(client), args.max_train_shards, not args.no_test)
        total = sum(f["size"] for f in files)
        print(f"AI4Mars: {len(files)} files, {total / 1e9:.2f} GB -> {args.out}", flush=True)
        for f in files:
            download(client, f, args.out)
    print("done", flush=True)


if __name__ == "__main__":
    sys.exit(main())
