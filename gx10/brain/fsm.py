"""Run state machine (doc 00 §7, P4 role doc §5).

SURVEY: for each station in station_plan: turn + drive there, `station`, score the proposals
with vision /score, then dedupe → exclusion → candidate rows → top-k ranks.
PLAN: nearest-neighbour route over the top-k → visit tasks; suggest mode waits for approve.
INVESTIGATE: nearest open top-k target first, re-planned from the new pose after every visit:
approach → scoop → /analyze (one retry on an empty scoop) → dump → mark_visited → add_analysis
→ exclusion → re-rank. Stops after k attempts, when nothing is left, on come_back or time up.
RETURN: drive home, face +x → end_run → mapper /fuse → DONE.
Controls: pause/resume (between steps), skip, come_back, stop, E-STOP, approve.
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
    AnalysisResult,
    AnalyzeReq,
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
    async def analyze(self, req: AnalyzeReq) -> AnalysisResult: ...


class Mapper(Protocol):
    async def fuse(self, run_id: str) -> dict: ...


# Candidate status after a visit, mirroring the mark_visited reducer.
STATUS_AFTER = {
    "sampled": "visited",
    "empty_scoop": "visited",
    "unreachable": "failed",
    "skipped": "skipped",
}


class FsmBusy(RuntimeError):
    pass


class NotNow(RuntimeError):
    """The command doesn't apply in the current state (e.g. skip while surveying)."""


class _ComeBack(Exception):
    pass


class RunFSM:
    def __init__(
        self,
        link: Link,
        vision: Vision,
        db: BrainDb,
        priors: Mapping[str, float],
        mapper: Mapper | None = None,
    ) -> None:
        self.link, self.vision, self.db, self.priors, self.mapper = link, vision, db, priors, mapper
        self.on_run_ended = lambda run_id: None
        self._target_task: asyncio.Task | None = None
        self._skip = self._come_back = False
        self._approval: asyncio.Future | None = None
        self._t0 = 0.0
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
        self.visit_tasks: dict[str, tuple[str, int]] = {}  # cand_id -> (task_id, seq)
        self.attempts = 0  # visits that count toward k; an operator skip is replaced instead
        self.plan_ids: list[str] = []  # the targets frozen at PLAN (+ replacements)
        self._next_seq = 1

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
        self._skip = self._come_back = False
        self._t0 = time.monotonic()
        self._task = asyncio.create_task(
            self._guarded(self._campaign()), name=f"campaign-{self.mission.mission_id}"
        )
        return self._task

    def skip(self) -> None:
        """Abandon the current target: recorded as a skipped visit, then re-route."""
        if self.state != "INVESTIGATE" or not self._target_task or self._target_task.done():
            raise NotNow(f"nothing to skip ({self.state})")
        self._skip = True
        self._target_task.cancel()

    def come_back(self) -> asyncio.Task | None:
        """Head home now: from SURVEY/INVESTIGATE (after the current step) or a stopped run."""
        if self.busy:
            if self.state == "AWAIT_APPROVAL" and self._approval and not self._approval.done():
                self._approval.set_result(False)
            self._come_back = True
            self._running.set()
            if self._target_task and not self._target_task.done():
                self._target_task.cancel()
            return self._task
        if self.state in ("STOPPED", "PLAN") and self.run_id:
            self._task = asyncio.create_task(self._guarded(self._return_home()), name="return")
            return self._task
        raise NotNow(f"no run to bring back ({self.state})")

    def approve(self, mission_id: str, approve: bool) -> None:
        if self.state != "AWAIT_APPROVAL" or not self._approval or self._approval.done():
            raise NotNow(f"nothing awaiting approval ({self.state})")
        if mission_id != self.mission.mission_id:
            raise NotNow(f"{mission_id} is not the mission awaiting approval")
        self._approval.set_result(approve)

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
        if self._come_back:
            raise _ComeBack

    def _transition(self, new: str, why: str = "") -> None:
        self.db.event("fsm", f"{self.state} -> {new}" + (f": {why}" if why else ""))
        self.state = new

    # --- campaign -------------------------------------------------------------------------
    async def _campaign(self) -> None:
        m = self.mission
        self.db.mission(m.model_dump(), "active")
        try:
            await self._survey()
            await self._plan()
            if m.mode == "suggest":
                self._approval = asyncio.get_running_loop().create_future()
                self._transition(
                    "AWAIT_APPROVAL", "suggest mode: approve the plan in the dashboard"
                )
                if not await self._approval and not self._come_back:
                    self.db.mission(m.model_dump(), "rejected")
                    self._transition("STOPPED", "plan rejected")
                    return
            await self._investigate()
        except _ComeBack:
            self.db.event("fsm", "come back requested")
        await self._return_home()

    async def _guarded(self, coro) -> None:
        try:
            await coro
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
        for xy in plan:
            sid, tid, seq = self.ids.next("s"), self.ids.next("t"), self._seq()
            self.db.task(tid, self.run_id, m.mission_id, "station", sid, seq, "pending")
            tasks.append((xy, sid, tid, seq))
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

    def _seq(self) -> int:
        self._next_seq += 1
        return self._next_seq - 1

    def _visit_task(self, cand_id: str) -> tuple[str, int]:
        if cand_id not in self.visit_tasks:
            tid, seq = self.ids.next("t"), self._seq()
            self.visit_tasks[cand_id] = (tid, seq)
            self.db.task(
                tid, self.run_id, self.mission.mission_id, "visit", cand_id, seq, "pending"
            )
        return self.visit_tasks[cand_id]

    def _open_topk(self) -> list[Candidate]:
        return [
            self.cands[cid] for _, cid in self.topk.items() if self.cands[cid].status == "queued"
        ]

    async def _plan(self) -> None:
        await self._checkpoint()
        p = self.pose
        self.route = planner.nn_route((p.x, p.y), self._open_topk())
        self.plan_ids = [c.cand_id for c in self.route]
        self._transition(
            "PLAN", f"top-{len(self.route)} route: " + " -> ".join(c.cand_id for c in self.route)
        )
        for c in self.route:
            self._visit_task(c.cand_id)

    # --- investigate ---------------------------------------------------------------------------
    async def _investigate(self) -> None:
        m = self.mission
        self._transition("INVESTIGATE", f"{'sample' if m.sample else 'look only'}, k={m.k}")
        while True:
            await self._checkpoint()
            if self.attempts >= m.k:
                break
            if time.monotonic() - self._t0 > m.max_duration_s:
                self.db.event("fsm", f"time up ({m.max_duration_s:g} s): heading home", "warn")
                break
            p = self.pose
            route = planner.nn_route((p.x, p.y), self._targets(m.k - self.attempts))
            if not route:
                self.db.event("fsm", "no targets left")
                break
            target = route[0]
            self._target_task = asyncio.create_task(self._visit(target))
            try:
                await self._target_task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():  # stop / E-STOP: cancel the whole run
                    self._target_task.cancel()
                    raise
                with contextlib.suppress(RoverError):
                    await self.link.cmd("stop", timeout_s=2.0)
                if self._skip:
                    self._skip = False
                    self._record_visit(target, "skipped", note="skipped by operator", counts=False)
                elif self._come_back:
                    self._requeue(target)
                    raise _ComeBack from None
            except _ComeBack:  # come_back hit a checkpoint inside the visit
                self._requeue(target)
                raise
            finally:
                self.active_cand = ""

    def _targets(self, slots: int) -> list[Candidate]:
        """Planned targets still open; if some dropped out (excluded, operator skip), top up
        with the best open candidates so the run still makes k attempts."""
        left = [self.cands[c] for c in self.plan_ids if self.cands[c].status in visited.OPEN]
        spare = sorted(
            (
                c
                for cid, c in self.cands.items()
                if c.status in visited.OPEN and cid not in self.plan_ids
            ),
            key=lambda c: (-c.score, c.cand_id),
        )
        for c in spare[: max(0, slots - len(left))]:
            self.plan_ids.append(c.cand_id)
            self._visit_task(c.cand_id)
            self.db.event("plan", f"{c.cand_id} added as a replacement target")
            left.append(c)
        return left

    async def _visit(self, c: Candidate) -> None:
        m = self.mission
        tid, seq = self._visit_task(c.cand_id)
        self.active_cand = c.cand_id
        self._set_status(c.cand_id, "active")
        self.db.task(tid, self.run_id, m.mission_id, "visit", c.cand_id, seq, "active")
        if not m.sample:
            await self._approach(c)
            await self._cmd("snapshot")
            self._record_visit(c, "skipped", note="look-only mission: photographed, not sampled")
            return
        sample_id = self.ids.next("a")
        analysis, image_url, outcome = None, "", "empty_scoop"
        for attempt in (1, 2):
            if not await self._approach(c):
                self._record_visit(c, "unreachable", note="lost target on approach")
                return
            await self._checkpoint()
            try:
                res = await self._cmd("scoop", {"sample_id": sample_id})
                image_url = res.get("present_image", "")
            except RoverError as e:
                if e.code != "E_SCOOP":
                    raise
                self.db.event("scoop", f"{c.cand_id}: scoop failed ({e.msg})", "warn")
                image_url = ""
            analysis = await self._analyze(c, sample_id, image_url) if image_url else None
            await self._cmd("dump")
            if analysis is not None and analysis.scoop_has_item:
                outcome = "sampled"
                break
            if attempt == 1:
                self.db.event("scoop", f"{c.cand_id}: empty scoop, retrying once", "warn")
        visit = self._record_visit(c, outcome)
        self.db.analysis(self.run_id, analysis, sample_id, c.cand_id, visit.visit_id, image_url)
        if analysis and analysis.assay:
            a = analysis.assay
            self.db.event(
                "analysis",
                f"{sample_id} on {c.cand_id}: SIMULATED assay "
                f"{a.concentration:g} {a.units} ({a.verdict})",
            )

    async def _approach(self, c: Candidate) -> bool:
        """True when the target is in the scoop zone; False on E_LOST_TARGET / not in zone."""
        await self._go_to_near(c.x, c.y)
        try:
            res = await self._cmd("approach", {"cand_id": c.cand_id, "x": c.x, "y": c.y})
        except RoverError as e:
            if e.code == "E_LOST_TARGET":
                return False
            raise
        return bool(res.get("in_zone", True))

    async def _go_to_near(self, x: float, y: float, standoff_m: float = 0.25) -> None:
        """Drive to within standoff_m of (x, y), facing it; approach does the last bit."""
        p = self.pose
        dist = math.hypot(x - p.x, y - p.y)
        if dist <= standoff_m:
            return
        f = (dist - standoff_m) / dist
        await self._go_to(p.x + (x - p.x) * f, p.y + (y - p.y) * f)

    async def _analyze(self, c: Candidate, sample_id: str, image_url: str) -> AnalysisResult | None:
        req = AnalyzeReq(
            run_id=self.run_id,
            sample_id=sample_id,
            cand_id=c.cand_id,
            image_url=image_url,
            interest_score=min(max(c.score, 0.0), 1.0),
        )
        try:
            return await self.vision.analyze(req)
        except Exception as e:
            self.db.event("vision", f"{sample_id}: /analyze failed ({e})", "error")
            return None

    def _set_status(self, cand_id: str, status: str, rank: int | None = None) -> None:
        c = self.cands[cand_id]
        upd = {"status": status} | ({"rank": rank} if rank is not None else {})
        self.cands[cand_id] = c = c.model_copy(update=upd)
        self.db.candidate(c, self.components[cand_id])

    def _requeue(self, c: Candidate) -> None:
        self._set_status(c.cand_id, "queued")
        tid, seq = self._visit_task(c.cand_id)
        self.db.task(tid, self.run_id, self.mission.mission_id, "visit", c.cand_id, seq, "pending")

    def _record_visit(
        self, c: Candidate, outcome: str, note: str = "", counts: bool = True
    ) -> Visit:
        p = self.pose
        self.attempts += counts
        visit = Visit(
            visit_id=self.ids.next("v"),
            run_id=self.run_id,
            cand_id=c.cand_id,
            x=c.x,
            y=c.y,
            t=time.time(),
            outcome=outcome,
        )
        self.visits.append(visit)
        self.db.visit(visit)  # the reducer also sets the candidate status
        self.cands[c.cand_id] = self.cands[c.cand_id].model_copy(
            update={"status": STATUS_AFTER[outcome], "rank": -1}
        )
        tid, seq = self._visit_task(c.cand_id)
        task_status = {"unreachable": "failed", "skipped": "skipped"}.get(outcome, "done")
        self.db.task(
            tid, self.run_id, self.mission.mission_id, "visit", c.cand_id, seq, task_status, note
        )
        self.db.event(
            "visit",
            f"{visit.visit_id}: {c.cand_id} {outcome}"
            + (f" ({note})" if note else "")
            + f" at ({p.x:.2f}, {p.y:.2f})",
        )
        for cid in visited.newly_excluded(self.cands, self.visits):
            self._set_status(cid, "excluded", rank=-1)
            self.db.event(
                "exclusion", f"{cid} excluded: within {visit.radius_m:g} m of {visit.visit_id}"
            )
        self._rerank()
        return visit

    # --- return --------------------------------------------------------------------------------
    async def _return_home(self) -> None:
        m = self.mission
        self._come_back = False
        self._transition("RETURN", "home" if m.return_home else "return_home=false: ending here")
        tid, seq = self.ids.next("t"), self._seq()
        self.db.task(tid, self.run_id, m.mission_id, "return", "home", seq, "active")
        if m.return_home:
            await self._go_to(0.0, 0.0)
            turn = wrap_deg(0.0 - self.pose.heading_deg)
            if abs(turn) > ALIGNED_DEG:
                await self._cmd("turn", {"deg": turn, "speed": TURN_SPEED})
        self.db.task(tid, self.run_id, m.mission_id, "return", "home", seq, "done")
        self.db.pose(self.run_id, self.pose, time.time())  # final pose, before writes stop
        self.db.mission(m.model_dump(), "done")
        self.db.run_ended(self.run_id, "done")
        self._transition("DONE", f"{len(self.visits)} visit(s)")
        if self.mapper is not None:
            try:
                res = await self.mapper.fuse(self.run_id)
                self.db.event("fuse", f"global map fused: {res.get('tiles', '?')} tiles")
            except Exception as e:
                self.db.event(
                    "fuse", f"mapper /fuse failed ({e}); replay uses local tiles", "error"
                )
        self.on_run_ended(self.run_id)
