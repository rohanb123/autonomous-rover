"""Run state machine (doc 00 §7). Built so far: SURVEY → PLAN, plus pause/resume/stop/E-STOP.

SURVEY: for each station in station_plan: turn + drive there, `station`, score the proposals
with vision /score, then dedupe → exclusion → candidate rows → top-k ranks.
PLAN: nearest-neighbour route over the top-k → visit tasks. INVESTIGATE / RETURN are next;
until then the run stops at PLAN and says so in the event log.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections.abc import Mapping
from typing import Protocol

from brain import dedupe, planner, scoring, visited
from brain.rover_link import RoverError
from brain.stdb_writer import BrainDb
from marsrover_shared.frames import wrap_deg
from marsrover_shared.ids import IdGen
from marsrover_shared.schemas import (
    Candidate,
    Mission,
    Pose,
    ScoreReqItem,
    ScoreRespItem,
    StationResult,
    Telemetry,
    Visit,
)

log = logging.getLogger(__name__)

DRIVE_SPEED, TURN_SPEED = 0.5, 0.5
ARRIVED_M, ALIGNED_DEG = 0.03, 2.0


class Link(Protocol):
    last_telemetry: Telemetry | None

    async def cmd(
        self, op: str, args: dict | None = None, timeout_s: float | None = None
    ) -> dict: ...
    async def estop(self) -> None: ...


class Vision(Protocol):
    async def score(self, run_id: str, items: list[ScoreReqItem]) -> list[ScoreRespItem]: ...


class FsmBusy(RuntimeError):
    pass


class RunFSM:
    def __init__(
        self, link: Link, vision: Vision, db: BrainDb, priors: Mapping[str, float]
    ) -> None:
        self.link, self.vision, self.db, self.priors = link, vision, db, priors
        self.state = "IDLE"
        self.active_cand = ""
        self.run_id = ""
        self._task: asyncio.Task | None = None
        self._running = asyncio.Event()
        self._running.set()
        self._paused_from = ""
        self._pose: Pose | None = None  # from the last done.result, with the time we got it
        self._pose_t = 0.0
        self._reset()

    def _reset(self) -> None:
        self.cands: dict[str, Candidate] = {}
        self.components: dict[str, dict] = {}
        self.embeddings: dict[str, list[float]] = {}
        self.visits: list[Visit] = []
        self.topk = planner.TopK(1)
        self.route: list[Candidate] = []

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def pose(self) -> Pose:
        """Latest known pose: a command's done.result, unless telemetry is newer.

        Telemetry lags a just-finished drive/turn, so planning the next leg from it alone
        compounds error; done.result.pose is authoritative at the moment it arrives.
        """
        tel = self.link.last_telemetry
        if self._pose is not None and (tel is None or tel.t <= self._pose_t):
            return self._pose
        return tel.pose if tel else Pose(x=0.0, y=0.0, heading_deg=0.0, source="dr")

    async def _cmd(self, op: str, args: dict | None = None) -> dict:
        result = await self.link.cmd(op, args)
        if isinstance(result.get("pose"), dict):
            self._pose, self._pose_t = Pose.model_validate(result["pose"]), time.time()
        return result

    # --- control -------------------------------------------------------------------------------
    def start_mission(self, mission: Mapping, ids: IdGen) -> asyncio.Task:
        if self.busy:
            raise FsmBusy("a mission is already running: stop it first")
        if self.state == "ESTOP":
            raise FsmBusy("E-STOP is latched: clear it first")
        self.mission = Mission.model_validate(mission)
        self.run_id, self.ids = self.mission.run_id, ids
        self._reset()
        self.topk = planner.TopK(self.mission.k)
        self._running.set()
        self._task = asyncio.create_task(
            self._campaign(), name=f"campaign-{self.mission.mission_id}"
        )
        return self._task

    def pause(self) -> None:
        if self.busy and self._running.is_set():
            self._running.clear()
            self._paused_from, self.state = self.state, "PAUSED"
            self.db.event("fsm", f"{self._paused_from} -> PAUSED (after the current step)")

    def resume(self) -> None:
        if self.state == "PAUSED":
            self.state = self._paused_from
            self._running.set()
            self.db.event("fsm", f"PAUSED -> {self.state}")

    async def stop(self) -> None:
        """Stop the campaign and the rover. Not queued behind the current step."""
        was = self.state
        await self._cancel()
        with contextlib.suppress(RoverError):
            await self.link.cmd("stop", timeout_s=2.0)
        self.state = "STOPPED"
        self.db.event("fsm", f"{was} -> STOPPED")

    async def estop(self) -> None:
        await self.link.estop()  # first, before anything else
        was = self.state
        await self._cancel()
        self.state = "ESTOP"
        self.db.event("estop", f"E-STOP latched (was {was})", "warn")

    async def clear_estop(self) -> None:
        await self.link.cmd("clear_estop", timeout_s=2.0)
        self.state = "STOPPED"
        self.db.event("estop", "E-STOP cleared -> STOPPED")

    async def _cancel(self) -> None:
        self._running.set()
        if self.busy:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _checkpoint(self) -> None:
        await self._running.wait()  # pause holds here, between steps

    def _transition(self, new: str, why: str = "") -> None:
        self.db.event("fsm", f"{self.state} -> {new}" + (f": {why}" if why else ""))
        self.state = new

    # --- campaign -------------------------------------------------------------------------
    async def _campaign(self) -> None:
        m = self.mission
        try:
            self.db.mission(m.model_dump(), "active")
            await self._survey()
            await self._plan()
        except RoverError as e:
            self.db.event("rover", f"Campaign stopped: rover error {e.code} ({e.msg})", "error")
            self.state = "STOPPED"
        except Exception as e:  # never let the campaign task die silently
            log.exception("campaign crashed")
            self.db.event("fsm", f"Campaign crashed: {e!r}", "error")
            self.state = "STOPPED"

    async def _survey(self) -> None:
        m = self.mission
        plan = planner.station_plan(m.radius_m)
        self._transition("SURVEY", f"{len(plan)} station(s), radius {m.radius_m:g} m")
        tasks = []
        for i, xy in enumerate(plan):
            sid, tid = self.ids.next("s"), self.ids.next("t")
            self.db.task(tid, self.run_id, m.mission_id, "station", sid, i + 1, "pending")
            tasks.append((xy, sid, tid, i + 1))
        for (x, y), sid, tid, seq in tasks:
            await self._checkpoint()
            self.db.task(tid, self.run_id, m.mission_id, "station", sid, seq, "active")
            await self._go_to(x, y)
            await self._checkpoint()
            raw = await self._cmd("station", {"station_id": sid, "n_views": 8, "step_deg": 45})
            await self._ingest_station(StationResult.model_validate(raw))
            self.db.task(tid, self.run_id, m.mission_id, "station", sid, seq, "done")

    async def _go_to(self, x: float, y: float) -> None:
        p = self.pose
        dist = math.hypot(x - p.x, y - p.y)
        if dist < ARRIVED_M:
            return
        turn = wrap_deg(math.degrees(math.atan2(y - p.y, x - p.x)) - p.heading_deg)
        if abs(turn) > ALIGNED_DEG:
            await self._cmd("turn", {"deg": turn, "speed": TURN_SPEED})
        await self._cmd("drive", {"dist_m": dist, "speed": DRIVE_SPEED})

    async def _ingest_station(self, st: StationResult) -> None:
        crops = st.mapper.urls.crops
        items = [
            ScoreReqItem(
                prop_id=p.prop_id, crop_url=crops.get(p.prop_id, ""), size_m=p.size_m, xy=p.xy
            )
            for p in st.proposals
        ]
        if not items:
            self.db.event("station", f"{st.station_id}: no proposals")
            return
        try:
            scored = await self.vision.score(self.run_id, items)
        except Exception as e:
            self.db.event(
                "vision", f"{st.station_id}: /score failed ({e}); proposals skipped", "error"
            )
            return
        by_id = {i.prop_id: i for i in items}
        new = merged = 0
        for s in scored:
            req = by_id.get(s.prop_id)
            if req is None:
                continue
            merged_into = self._add_candidate(st, s, req)
            new, merged = (new, merged + 1) if merged_into else (new + 1, merged)
        self._rerank()
        self.db.event(
            "station", f"{st.station_id}: {len(scored)} scored, {new} new, {merged} merged"
        )

    def _add_candidate(self, st: StationResult, s: ScoreRespItem, req: ScoreReqItem) -> str | None:
        m = self.mission
        item = {**s.model_dump(), "xy": req.xy, "size_m": req.size_m}
        score, comps = scoring.final_score(item, m, self.priors, st.pose)
        cand = Candidate(
            cand_id="",
            run_id=self.run_id,
            station_id=st.station_id,
            prop_ids=[s.prop_id],
            x=req.xy[0],
            y=req.xy[1],
            score=score,
            novelty=s.novelty,
            visual=s.visual,
            pickable=s.pickable,
            category_score=comps["category_score"],
            distance_m=comps["distance_m"],
            category=s.category,
            size_m=req.size_m,
            status="new",
            rank=-1,
            thumb_url=req.crop_url,
            reason=scoring.explain(comps, s.category, m.categories, comps["distance_m"]),
        )
        into = dedupe.merge_or_new(
            {"xy": req.xy, "embedding": s.embedding}, self.cands, self.embeddings
        )
        if into:
            cand = dedupe.merge(
                self.cands[into], s.prop_id, cand.model_copy(update={"cand_id": into})
            )
            if cand.score >= self.cands[into].score:
                self.components[into] = comps
        else:
            into_id = self.ids.next("c")
            cand = cand.model_copy(update={"cand_id": into_id})
            self.embeddings[into_id] = s.embedding
            self.components[into_id] = comps
            if visited.excluded(req.xy, self.visits):
                cand = cand.model_copy(update={"status": "excluded"})
        self.cands[cand.cand_id] = cand
        self.db.candidate(cand, self.components[cand.cand_id])
        return into

    def _rerank(self) -> None:
        """Rebuild the top-k from all open candidates; write only rows whose rank/status changed."""
        self.topk.rebuild(self.cands)
        ranks = self.topk.ranks()
        for cid, c in self.cands.items():
            if c.status not in visited.OPEN:
                continue
            rank = ranks.get(cid, -1)
            status = "queued" if rank > 0 else "new"
            if (rank, status) != (c.rank, c.status):
                self.cands[cid] = c = c.model_copy(update={"rank": rank, "status": status})
                self.db.candidate(c, self.components[cid])

    async def _plan(self) -> None:
        await self._checkpoint()
        m = self.mission
        targets = [self.cands[cid] for _, cid in self.topk.items()]
        p = self.pose
        self.route = planner.nn_route((p.x, p.y), targets)
        self._transition(
            "PLAN", f"top-{len(self.route)} route: " + " -> ".join(c.cand_id for c in self.route)
        )
        seq0 = len(planner.station_plan(m.radius_m))
        for i, c in enumerate(self.route):
            self.db.task(
                self.ids.next("t"),
                self.run_id,
                m.mission_id,
                "visit",
                c.cand_id,
                seq0 + i + 1,
                "pending",
            )
        if m.mode == "suggest":
            self._transition("AWAIT_APPROVAL", "suggest mode: approve the plan in the dashboard")
        else:
            self.db.event("fsm", "INVESTIGATE is not built yet: the run stops at PLAN", "warn")
