"""Download the ChemCam master observation list (docs/03 §2-§3).

Source: PDS Geosciences Node, MSL ChemCam LIBS RDR bundle, document/msl_ccam_obs.csv (~26 MB)
plus its PDS3 label msl_ccam_obs.lbl (column definitions). Public PDS data.
About 123,514 rows x 34 columns (Sol, Target, Target Type, Comments, ...), sols 10-4843.

PDS publishes no checksum for these files, so the script checks the size the server reports
and that the CSV parses with a header row. Files go to ml/data/chemcam/ (gitignored).

Usage:  python ml/download/get_chemcam_obs.py [--out DIR]
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fetch import TIMEOUT, fetch  # noqa: E402

BASE = "https://pds-geosciences.wustl.edu/msl/msl-m-chemcam-libs-4_5-rdr-v1/mslccm_1xxx/document/"
FILES = ["msl_ccam_obs.lbl", "msl_ccam_obs.csv"]
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "data" / "chemcam"


def summarize(csv_path: Path) -> tuple[int, list[str]]:
    with csv_path.open(newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = sum(1 for _ in reader)
    return rows, header


def main() -> None:
    ap = argparse.ArgumentParser(description="Download the ChemCam observation list.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()

    with httpx.Client(follow_redirects=True, timeout=TIMEOUT) as client:
        print(f"ChemCam observation list -> {args.out}", flush=True)
        for name in FILES:
            head = client.head(BASE + name)
            head.raise_for_status()
            size = int(head.headers["content-length"]) if "content-length" in head.headers else None
            fetch(client, BASE + name, args.out / name, size)

    rows, header = summarize(args.out / "msl_ccam_obs.csv")
    print(f"  csv: {rows} rows x {len(header)} columns", flush=True)
    print(f"  columns: {', '.join(h.strip() for h in header)}", flush=True)
    print("done", flush=True)


if __name__ == "__main__":
    main()
