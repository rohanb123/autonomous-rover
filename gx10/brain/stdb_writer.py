"""BrainDb: the brain's writes to SpacetimeDB, through RateLimitedWriter (docs/02 §8).

Never call STDB on the control path: every method only queues. Positional arg order is
docs/02 §6. Every event also goes to events.jsonl, the backup if the DB is down.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping

from marsrover_shared import eventlog
from marsrover_shared.stdb_http import RateLimitedWriter, StdbClient

RATES_HZ = {"pose": 2.0, "status": 2.0, "telemetry": 1.0}


class BrainDb:
    def __init__(self, client: StdbClient) -> None:
        self._run_id = ""
        self._w = RateLimitedWriter(
            client, RATES_HZ, on_drop=lambda reason, n: self._backpressure(reason, n)
        )

    def _backpressure(self, reason: str, dropped: int) -> None:
        eventlog.log(
            self._run_id or "no_run",
            "brain",
            "warn",
            "stdb_backpressure",
            reason,
            {"dropped": dropped},
        )

    def close(self) -> None:
        self._w.close()

    # --- runs and missions ----------------------------------------------------------------
    def run_started(self, run_id: str, notes: str) -> None:
        self._run_id = run_id
        self._w.submit(f"run:{run_id}", "start_run", run_id, notes, time.time())

    def run_ended(self, run_id: str, status: str = "done") -> None:
        self._w.submit(f"run_end:{run_id}", "end_run", run_id, status, time.time())

    def mission(self, m: Mapping, status: str) -> None:
        params = {k: m[k] for k in ("mission_id", "run_id", "text", "radius_m", "k", "categories",
                                    "sample", "return_home", "max_duration_s", "mode")}  # fmt: skip
        self._w.submit(
            f"mission:{m['mission_id']}:{status}", "upsert_mission", m["mission_id"], m["run_id"],
            m["text"], json.dumps(params), json.dumps(m.get("assumptions", [])), status,
        )  # fmt: skip

    # --- events ------------------------------------------------------------------------------
    def event(self, kind: str, msg: str, level: str = "info", data: Mapping | None = None) -> None:
        run_id = self._run_id or "no_run"
        payload = dict(data or {})
        eventlog.log(run_id, "brain", level, kind, msg, payload)
        if self._run_id:
            self._w.submit(f"event:{time.time_ns()}", "log_event", run_id, time.time(), "brain",
                           level, kind, msg, json.dumps(payload))  # fmt: skip
