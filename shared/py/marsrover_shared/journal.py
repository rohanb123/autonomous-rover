"""Per-run journal of SpacetimeDB writes: data/runs/<run_id>/stdb_calls.jsonl.

Every reducer call a writer submits is appended (in submit order) before it reaches the DB, so
a run can be rebuilt in a fresh database with mocks/replay_run.py if STDB dies mid-run.
events.jsonl stays the human-readable log; this file is the machine-replayable one.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from marsrover_shared.eventlog import data_root

FILE = "stdb_calls.jsonl"

# Position of runId in each reducer's positional args (docs/02 §6). Reducers not listed
# (claim_writer, add_writer) are setup, not run data, and aren't journaled.
RUN_ID_ARG = {
    "start_run": 0,
    "end_run": 0,
    "set_run_fused": 0,
    "insert_pose": 0,
    "insert_telemetry": 0,
    "upsert_rover_status": 0,
    "add_analysis": 0,
    "log_event": 0,
    "upsert_mission": 1,
    "upsert_task": 1,
    "upsert_keyframe": 1,
    "upsert_local_tile": 1,
    "upsert_global_tile": 1,
    "upsert_candidate": 1,
    "mark_visited": 1,
}


def run_of(reducer: str, args: Sequence[Any]) -> str:
    """The run a call belongs to, or "" (not journaled)."""
    i = RUN_ID_ARG.get(reducer)
    if i is None or i >= len(args) or not isinstance(args[i], str):
        return ""
    return args[i]


def path(run_id: str) -> Path:
    return data_root() / "runs" / run_id / FILE


def record(reducer: str, args: Sequence[Any]) -> None:
    """Append one call. One write() on an O_APPEND fd, so brain + mapper can share the file."""
    run_id = run_of(reducer, args)
    if not run_id:
        return
    p = path(run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"t": time.time(), "reducer": reducer, "args": list(args)}) + "\n"
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def read(run_id: str) -> list[dict]:
    p = path(run_id)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
