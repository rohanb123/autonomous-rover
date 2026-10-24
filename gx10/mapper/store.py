"""On-disk run store for the mapper (docs/00 §1: big files live on the mapper disk, URLs in the DB).

Layout (P3 owns it, docs/00 §2 "Runtime data/"), relative to the data root:
    runs/<run_id>/stations/<station_id>/station.json, kf_*.jpg, bev.png, crop_p_*.jpg
    runs/<run_id>/samples/<sample_id>/sample.json, present.jpg
    runs/<run_id>/events.jsonl                      (appended by marsrover_shared.eventlog)

The data root is $MARSROVER_DATA_DIR, else the repo's data/ (a symlink to /data on the GX10).
URLs are <public_url>/files/<path relative to the data root>.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from marsrover_shared import eventlog

DEFAULT_PUBLIC_URL = "http://192.168.50.10:8200"  # docs/00 §3

_SAFE_ID = re.compile(r"^[a-z]_[0-9_]{4,32}$")  # r_YYYYMMDD_HHMMSS, s_0001, a_0001 (AGENTS.md §4)
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


class StoreError(ValueError):
    """Bad id or file name (would escape the run folder or isn't an AGENTS.md §4 id)."""


def check_id(value: str, what: str = "id") -> str:
    if not _SAFE_ID.match(value):
        raise StoreError(f"bad {what}: {value!r}")
    return value


def check_name(value: str) -> str:
    if not _SAFE_NAME.match(value):
        raise StoreError(f"bad file name: {value!r}")
    return value


class RunStore:
    def __init__(self, root: str | Path | None = None, public_url: str | None = None) -> None:
        """root: the data root (default $MARSROVER_DATA_DIR or repo data/); runs live in root/runs.
        public_url: base for file URLs (default $MAPPER_PUBLIC_URL or the GX10 address)."""
        self.root = Path(root) if root is not None else eventlog.data_root()
        url = public_url or os.environ.get("MAPPER_PUBLIC_URL") or DEFAULT_PUBLIC_URL
        self.public_url = url.rstrip("/")

    # --- paths and URLs ---------------------------------------------------------------

    def url_for(self, rel_path: str) -> str:
        """http://<mapper>/files/runs/... for a path relative to the data root."""
        return f"{self.public_url}/files/{rel_path.lstrip('/')}"

    def resolve(self, rel_path: str) -> Path | None:
        """Absolute path of an existing file under the data root, or None (also on traversal)."""
        base = self.root.resolve()
        target = (base / rel_path).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            return None
        return target

    @staticmethod
    def station_dir(run_id: str, station_id: str) -> str:
        return f"runs/{check_id(run_id, 'run_id')}/stations/{check_id(station_id, 'station_id')}"

    @staticmethod
    def sample_dir(run_id: str, sample_id: str) -> str:
        return f"runs/{check_id(run_id, 'run_id')}/samples/{check_id(sample_id, 'sample_id')}"

    # --- writes -----------------------------------------------------------------------

    def _write_dir(self, rel_dir: str, meta_name: str, meta: dict, files: dict[str, bytes]) -> str:
        out = self.root / rel_dir
        out.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            (out / check_name(name)).write_bytes(data)
        # Metadata last: its presence means the bundle is complete (a Pi retry overwrites all).
        tmp = out / f".{meta_name}.tmp"
        tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, out / meta_name)
        return rel_dir

    def save_station(self, run_id: str, station: dict, files: dict[str, bytes]) -> str:
        """Store a station bundle; returns its folder relative to the data root."""
        rel = self.station_dir(run_id, station["station_id"])
        return self._write_dir(rel, "station.json", station, files)

    def save_sample(self, run_id: str, sample: dict, jpg: bytes) -> str:
        """Store a scoop photo; returns present.jpg's path relative to the data root."""
        rel = self.sample_dir(run_id, sample["sample_id"])
        self._write_dir(rel, "sample.json", sample, {"present.jpg": jpg})
        return f"{rel}/present.jpg"

    def has_station(self, run_id: str, station_id: str) -> bool:
        return (self.root / self.station_dir(run_id, station_id) / "station.json").is_file()
