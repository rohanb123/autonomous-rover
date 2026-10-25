"""BrainDb: the brain's writes to SpacetimeDB, through RateLimitedWriter (docs/02 §8).

Never call STDB on the control path: every method only queues. Positional arg order is
docs/02 §6. Every event also goes to events.jsonl, the backup if the DB is down.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping

from marsrover_shared import eventlog, journal
from marsrover_shared.schemas import AnalysisResult, Candidate, Pose, Telemetry, Visit
from marsrover_shared.stdb_http import RateLimitedWriter, StdbClient

RATES_HZ = {"pose": 2.0, "status": 2.0, "telemetry": 1.0}


class BrainDb:
    def __init__(self, client: StdbClient) -> None:
        self._run_id = ""
        self._w = RateLimitedWriter(
            client,
            RATES_HZ,
            on_drop=lambda reason, n: self._backpressure(reason, n),
            journal=journal.record,  # every write -> data/runs/<run>/stdb_calls.jsonl
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
        params = {
            k: m[k]
            for k in (
                "mission_id",
                "run_id",
                "text",
                "radius_m",
                "k",
                "categories",
                "sample",
                "return_home",
                "max_duration_s",
                "mode",
            )
        }
        self._w.submit(
            f"mission:{m['mission_id']}:{status}",
            "upsert_mission",
            m["mission_id"],
            m["run_id"],
            m["text"],
            json.dumps(params),
            json.dumps(m.get("assumptions", [])),
            status,
        )

    def task(
        self,
        task_id: str,
        run_id: str,
        mission_id: str,
        kind: str,
        ref: str,
        seq: int,
        status: str,
        note: str = "",
    ) -> None:
        self._w.submit(
            f"task:{task_id}:{time.time_ns()}",
            "upsert_task",
            task_id,
            run_id,
            mission_id,
            kind,
            ref,
            seq,
            status,
            note,
        )

    def candidate(self, c: Candidate, components: Mapping) -> None:
        comps = {**components, "prop_ids": c.prop_ids}
        self._w.submit(
            f"cand:{c.cand_id}:{time.time_ns()}",
            "upsert_candidate",
            c.cand_id,
            c.run_id,
            c.station_id,
            c.x,
            c.y,
            round(c.score, 4),
            json.dumps(comps),
            c.category,
            c.size_m,
            c.status,
            c.rank,
            c.reason,
            c.thumb_url,
            time.time(),
        )

    def visit(self, v: Visit) -> None:
        self._w.submit(
            f"visit:{v.visit_id}",
            "mark_visited",
            v.visit_id,
            v.run_id,
            v.cand_id,
            v.x,
            v.y,
            v.radius_m,
            v.outcome,
            v.t,
        )

    def analysis(
        self,
        run_id: str,
        a: AnalysisResult | None,
        sample_id: str,
        cand_id: str,
        visit_id: str,
        image_url: str,
    ) -> None:
        """add_analysis (docs/02 §6). No assay -> the documented sentinels."""
        t = time.time()
        head = [run_id, sample_id, cand_id, visit_id, t, image_url]
        if a is None:
            body = ["", 0.0, "", False]
        else:
            body = [a.label, a.confidence, a.caption]
        if a is not None and a.assay is not None:
            s = a.assay
            assay = [
                s.assay_type,
                s.a595,
                s.concentration,
                s.units,
                s.verdict,
                s.in_linear_range,
                s.standard_curve_params.model_dump_json(),
                True,
                s.seed,
                s.model_version,
            ]
        else:
            assay = ["", -1.0, -1.0, "", "", False, "{}", True, 0, ""]
        self._w.submit(
            f"analysis:{sample_id}:{time.time_ns()}", "add_analysis", *head, *body, *assay
        )

    # --- rover state (coalesced: pose/status 2 Hz, telemetry 1 Hz) -----------------------------
    def pose(self, run_id: str, pose: Pose, t: float) -> None:
        sigma = pose.sigma_xy_m if pose.sigma_xy_m is not None else -1.0
        self._w.submit(
            "pose", "insert_pose", run_id, t, pose.x, pose.y, pose.heading_deg, pose.source, sigma
        )

    def telemetry(self, run_id: str, tel: Telemetry) -> None:
        self._w.submit(
            "telemetry",
            "insert_telemetry",
            run_id,
            tel.t,
            tel.state,
            -1.0,
            tel.cpu_temp_c,
            tel.imu.model_dump_json(),
            tel.range_m if tel.range_m is not None else -1.0,
            tel.uplink_backlog,
        )

    def status(
        self,
        run_id: str,
        state: str,
        estop: bool,
        pose: Pose | None,
        fsm_state: str,
        active_cand: str,
        link_ok: bool,
    ) -> None:
        x, y, h = (pose.x, pose.y, pose.heading_deg) if pose else (0.0, 0.0, 0.0)
        self._w.submit(
            "status",
            "upsert_rover_status",
            run_id,
            time.time(),
            state,
            estop,
            x,
            y,
            h,
            fsm_state,
            active_cand,
            link_ok,
        )

    # --- events ------------------------------------------------------------------------------
    def event(self, kind: str, msg: str, level: str = "info", data: Mapping | None = None) -> None:
        run_id = self._run_id or "no_run"
        payload = dict(data or {})
        eventlog.log(run_id, "brain", level, kind, msg, payload)
        if self._run_id:
            self._w.submit(
                f"event:{time.time_ns()}",
                "log_event",
                run_id,
                time.time(),
                "brain",
                level,
                kind,
                msg,
                json.dumps(payload),
            )
