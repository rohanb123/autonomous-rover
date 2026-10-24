"""Append-only events.jsonl per run (doc 00 §6.13, AGENTS.md §4 Logging).

Path: <data root>/runs/<run_id>/events.jsonl. The data root is $MARSROVER_DATA_DIR,
else the repo's data/ (a symlink to /data on the GX10).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from marsrover_shared.schemas import EventLogLine

_REPO_DATA = Path(__file__).resolve().parents[3] / "data"


def data_root() -> Path:
    return Path(os.environ.get("MARSROVER_DATA_DIR", _REPO_DATA))


def events_path(run_id: str) -> Path:
    return data_root() / "runs" / run_id / "events.jsonl"


def log(
    run_id: str,
    src: str,
    level: str,
    kind: str,
    msg: str,
    data: dict[str, Any] | None = None,
) -> None:
    line = EventLogLine(
        t=time.time(), run_id=run_id, src=src, level=level, kind=kind, msg=msg, data=data or {}
    )
    path = events_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (line.model_dump_json() + "\n").encode()
    # One write() on an O_APPEND fd, so concurrent services don't interleave lines.
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def read(run_id: str) -> list[EventLogLine]:
    path = events_path(run_id)
    if not path.exists():
        return []
    return [
        EventLogLine.model_validate(json.loads(ln)) for ln in path.read_text().splitlines() if ln
    ]
