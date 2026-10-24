"""Download and unpack the MSL Curiosity labeled image set v2.1 (docs/03 §2).

Source: Zenodo record 4033453, `msl-labeled-data-set-v2.1.zip` (54.9 MB, CC-BY-4.0).
6,820 images (227×227), 19 classes, splits train 5,920 / val 300 / test 600.
Attribution (docs/03 §6): Wagstaff et al., MSL labeled data set v2.1, Zenodo 4033453.

- Zip goes to ml/data/msl_v21/msl-labeled-data-set-v2.1.zip (gitignored), md5-verified against
  the checksum Zenodo publishes; it's unpacked next to it. Nothing downloaded is executed.
- Unpacking refuses any entry whose path would land outside the target folder.

Usage:  python ml/download/get_msl_v21.py [--out DIR] [--no-extract]
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fetch import TIMEOUT, fetch  # noqa: E402

RECORD_API = "https://zenodo.org/api/records/4033453"
FILE_KEY = "msl-labeled-data-set-v2.1.zip"
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "msl_v21"


def safe_extract(zip_path: Path, out_dir: Path) -> int:
    """Unpack zip_path into out_dir, rejecting absolute paths and '..' escapes."""
    base = out_dir.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            target = (base / info.filename).resolve()
            if not target.is_relative_to(base):
                raise SystemExit(f"unsafe path in zip: {info.filename!r}")
        zf.extractall(base)
        return len(zf.infolist())


def main() -> None:
    ap = argparse.ArgumentParser(description="Download the MSL labeled data set v2.1.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--no-extract", action="store_true")
    args = ap.parse_args()

    with httpx.Client(follow_redirects=True, timeout=TIMEOUT) as client:
        resp = client.get(RECORD_API)
        resp.raise_for_status()
        files = {f["key"]: f for f in resp.json()["files"]}
        if FILE_KEY not in files:
            raise SystemExit(f"{FILE_KEY} not in Zenodo record; found {sorted(files)}")
        f = files[FILE_KEY]
        print(f"MSL v2.1: {f['size'] / 1e6:.1f} MB -> {args.out}", flush=True)
        zip_path = fetch(client, f["links"]["self"], args.out / FILE_KEY, f["size"], f["checksum"])

    if not args.no_extract:
        n = safe_extract(zip_path, args.out)
        print(f"  unpacked {n} entries into {args.out}", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
