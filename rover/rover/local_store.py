"""SD-card bundles + a SQLite index: the Pi's buffer and backup (doc 00 section 1 table).

    <root>/<run_id>/stations/<station_id>/station.json, kf_*.jpg, bev.png, crop_p_*.jpg
    <root>/<run_id>/samples/<sample_id>/sample.json, closeup.jpg
    <root>/index.sqlite        one row per bundle: uploaded? attempts, last error

The index is what makes the upload queue survive a reboot or a service restart: the retry
loop asks `pending_uploads()` rather than holding the queue in memory, so a station captured
while the GX10 was unreachable still reaches the mapper after the Pi has been power-cycled.

Each bundle is written to a temp directory and renamed into place, so a bundle the index can
see is always complete -- a brown-out mid-write leaves a `.tmp-*` directory, never a station
missing its bev.png.

Stdlib + pydantic only. Thread-safe: the op worker saves, the uploader's thread marks.
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from marsrover_shared.schemas import SampleJson, StationJson

Kind = Literal["station", "sample"]

#: The multipart field each bundle's metadata travels as (doc 00 sections 6.4-6.5).
META_NAME: dict[Kind, str] = {"station": "station.json", "sample": "sample.json"}

# uploaded: 0 = pending, 1 = done, -1 = rejected by the mapper (never retried)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS bundles (
    path     TEXT PRIMARY KEY,
    kind     TEXT NOT NULL,
    run_id   TEXT NOT NULL,
    ref      TEXT NOT NULL,
    created  REAL NOT NULL,
    uploaded INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_err TEXT NOT NULL DEFAULT ''
)
"""


class StoreError(RuntimeError):
    """A bundle on disk is missing or unreadable."""


@dataclass(frozen=True)
class Bundle:
    """One saved bundle, read back for upload. `path` is relative to the store root."""

    path: str
    kind: Kind
    meta_json: str
    files: Mapping[str, bytes]

    @property
    def meta_name(self) -> str:
        return META_NAME[self.kind]


class LocalStore:
    def __init__(self, root: str | Path = "data/rover") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.root / "index.sqlite", check_same_thread=False)
        with self._tx() as db:
            db.execute(_SCHEMA)

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._db:  # the connection as a context manager commits/rolls back
            yield self._db

    # --- writing -------------------------------------------------------------------------

    def save_station(self, station: StationJson, files: Mapping[str, bytes]) -> str:
        rel = f"{station.run_id}/stations/{station.station_id}"
        return self._save(
            rel, "station", station.run_id, station.station_id, station.model_dump_json(), files
        )

    def save_sample(self, sample: SampleJson, jpg: bytes) -> str:
        rel = f"{sample.run_id}/samples/{sample.sample_id}"
        return self._save(
            rel,
            "sample",
            sample.run_id,
            sample.sample_id,
            sample.model_dump_json(),
            {"closeup.jpg": jpg},  # the part name the mapper requires (doc 00 section 6.5)
        )

    def _save(
        self,
        rel: str,
        kind: Kind,
        run_id: str,
        ref: str,
        meta_json: str,
        files: Mapping[str, bytes],
    ) -> str:
        for name in files:
            if Path(name).name != name or name == META_NAME[kind]:
                raise ValueError(f"bad bundle file name {name!r}")
        final = self.root / rel
        tmp = final.parent / f".tmp-{uuid.uuid4().hex}"
        tmp.mkdir(parents=True)
        try:
            (tmp / META_NAME[kind]).write_text(meta_json, encoding="utf-8")
            for name, data in files.items():
                (tmp / name).write_bytes(data)
            if final.exists():  # a re-save of the same id replaces it wholesale
                shutil.rmtree(final)
            tmp.rename(final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        with self._tx() as db:
            db.execute(
                "INSERT OR REPLACE INTO bundles (path, kind, run_id, ref, created) "
                "VALUES (?, ?, ?, ?, ?)",
                (rel, kind, run_id, ref, time.time()),
            )
        return rel

    # --- reading -------------------------------------------------------------------------

    def load(self, rel: str) -> Bundle:
        with self._tx() as db:
            row = db.execute("SELECT kind FROM bundles WHERE path = ?", (rel,)).fetchone()
        if row is None:
            raise StoreError(f"{rel}: not in the index")
        kind: Kind = row[0]
        folder = self.root / rel
        meta = folder / META_NAME[kind]
        try:
            meta_json = meta.read_text(encoding="utf-8")
            files = {
                p.name: p.read_bytes()
                for p in sorted(folder.iterdir())
                if p.is_file() and p.name != META_NAME[kind]
            }
        except OSError as e:
            raise StoreError(f"{rel}: {e}") from e
        return Bundle(rel, kind, meta_json, files)

    def pending_uploads(self) -> list[str]:
        """Oldest first, so the mapper sees stations in capture order."""
        with self._tx() as db:
            rows = db.execute(
                "SELECT path FROM bundles WHERE uploaded = 0 ORDER BY created, path"
            ).fetchall()
        return [r[0] for r in rows]

    @property
    def backlog(self) -> int:
        with self._tx() as db:
            return db.execute("SELECT COUNT(*) FROM bundles WHERE uploaded = 0").fetchone()[0]

    def status(self, rel: str) -> tuple[int, int, str]:
        """(uploaded, attempts, last_err); for tests and debugging."""
        with self._tx() as db:
            row = db.execute(
                "SELECT uploaded, attempts, last_err FROM bundles WHERE path = ?", (rel,)
            ).fetchone()
        if row is None:
            raise StoreError(f"{rel}: not in the index")
        return row

    # --- marking -------------------------------------------------------------------------

    def mark_uploaded(self, rel: str) -> None:
        self._mark(rel, 1, "")

    def mark_failed(self, rel: str, err: str) -> None:
        self._mark(rel, 0, err)

    def mark_rejected(self, rel: str, err: str) -> None:
        """The mapper refused it (4xx) or it is unreadable: stop retrying, keep the files."""
        self._mark(rel, -1, err)

    def _mark(self, rel: str, uploaded: int, err: str) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE bundles SET uploaded = ?, attempts = attempts + 1, last_err = ? "
                "WHERE path = ?",
                (uploaded, err[:500], rel),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()
