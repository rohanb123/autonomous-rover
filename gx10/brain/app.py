"""Brain service, FastAPI :8000: the dashboard's only command API (doc 00 §6.11).

Runs; text and voice missions (Grok with the offline backup) that start the full campaign
(SURVEY -> PLAN -> INVESTIGATE -> RETURN); pause / resume / skip / come_back / stop /
E-STOP / approve; health.

Run (from gx10/):  uvicorn brain.app:app --port 8000
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from brain import settings
from brain.fsm import FsmBusy, NotNow, RunFSM
from brain.overhead_link import OverheadLink
from brain.rover_link import RoverError, RoverLink
from brain.stdb_writer import BrainDb
from brain.vision_client import MapperClient, VisionClient, load_priors
from brain.voice import asr, nlu
from marsrover_shared.ids import IdGen, new_run_id
from marsrover_shared.schemas import (
    ApproveReq,
    CommandAccepted,
    HealthResp,
    MissionTextReq,
    QuickReq,
    RunStartReq,
)
from marsrover_shared.stdb_http import StdbClient

log = logging.getLogger("brain")
NETWORK = yaml.safe_load((settings.ROOT / "config" / "network.yaml").read_text(encoding="utf-8"))


def _url(env_key: str, service: str) -> str:
    svc = NETWORK["services"][service]
    host = NETWORK["hosts"][svc["host"]]["ip"]
    return settings.env(env_key, f"http://{host}:{svc['port']}")


class Brain:
    """Process-wide state. One run at a time."""

    def __init__(self, db: BrainDb, link: RoverLink, fsm: RunFSM, overhead: OverheadLink) -> None:
        self.db, self.link, self.fsm, self.overhead = db, link, fsm, overhead
        self.run_id = ""
        self.ids = IdGen()
        self.missions: dict[str, dict] = {}

    def accepted(self) -> CommandAccepted:
        return CommandAccepted(accepted=True, cmd_uid=self.ids.next("u"))

    def require_run(self) -> str:
        if not self.run_id:
            raise HTTPException(409, "No active run: start one first (POST /api/run/start).")
        return self.run_id


def _ws_url(env_key: str, service: str) -> str:
    svc = NETWORK["services"][service]
    host = NETWORK["hosts"][svc["host"]]["ip"]
    return settings.env(env_key, f"ws://{host}:{svc['port']}{svc.get('path', '')}")


async def _status_loop(b: Brain) -> None:
    """rover_status at 2 Hz even when the link is down, so the dashboard can tell."""
    while True:
        tel = b.link.last_telemetry if b.link.connected else None
        estop = bool(tel and tel.estop) or b.fsm.state == "ESTOP"
        state = tel.state if tel else "UNKNOWN"
        pose = tel.pose if tel else None
        b.db.status(b.run_id, state, estop, pose, b.fsm.state, b.fsm.active_cand, b.link.connected)
        await asyncio.sleep(0.5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.load_env()
    db = BrainDb(StdbClient.from_env("STDB_TOKEN_BRAIN"))
    link = RoverLink(_ws_url("ROVER_URL", "rover_ws"))
    vision = VisionClient(_url("VISION_URL", "vision"))
    mapper = MapperClient(_url("MAPPER_URL", "mapper"))
    overhead = OverheadLink(_ws_url("OVERHEAD_WS_URL", "overhead"))
    b = Brain(db, link, RunFSM(link, vision, db, load_priors(), mapper, overhead), overhead)

    def on_run_ended(run_id: str) -> None:
        if b.run_id == run_id:
            b.run_id = ""

    b.fsm.on_run_ended = on_run_ended

    def on_telemetry(tel):
        if b.run_id:
            db.pose(b.run_id, tel.pose, tel.t)
            db.telemetry(b.run_id, tel)

    link.on_telemetry = on_telemetry
    link.on_event = lambda e: db.event("rover", f"rover event {e.kind}", "warn", e.detail)
    link.on_connection = lambda up: db.event(
        "rover", f"rover link {'up' if up else 'down'}", "info" if up else "warn"
    )
    app.state.brain = b
    link.start()
    overhead.start()
    status = asyncio.create_task(_status_loop(b))
    yield
    status.cancel()
    await link.stop()
    await overhead.stop()
    await vision.close()
    await mapper.close()
    db.close()


app = FastAPI(title="ares-scout brain", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=NETWORK["cors_origins"] + ["http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def brain_of(request: Request) -> Brain:
    return request.app.state.brain


def brain_of_app() -> Brain:
    return app.state.brain


# --- health ----------------------------------------------------------------------------------


async def _probe(client: httpx.AsyncClient, url: str, any_reply_ok: bool = False) -> str:
    try:
        resp = await client.get(url, timeout=1.0)
        return "ok" if resp.is_success or any_reply_ok else "degraded"
    except httpx.HTTPError:
        return "down"


@app.get("/api/health", response_model=HealthResp)
async def health() -> HealthResp:
    async with httpx.AsyncClient() as client:
        stdb, vision, mapper = await asyncio.gather(
            _probe(client, settings.env("STDB_URL", "http://192.168.50.10:3000") + "/v1/ping"),
            _probe(client, _url("VISION_URL", "vision") + "/health"),
            _probe(client, _url("MAPPER_URL", "mapper") + "/health"),
        )
    return HealthResp(
        brain="ok",
        rover_link="ok" if brain_of_app().link.connected else "down",
        vision=vision,
        mapper=mapper,
        stdb=stdb,
        overhead=brain_of_app().overhead.health,
    )


# --- runs --------------------------------------------------------------------------------------


@app.post("/api/run/start", response_model=CommandAccepted)
async def run_start(req: RunStartReq, request: Request) -> CommandAccepted:
    b = brain_of(request)
    b.run_id = new_run_id(datetime.now())
    b.ids = IdGen()
    b.db.run_started(b.run_id, req.notes or "")
    b.db.event("run", f"Run {b.run_id} started")
    return b.accepted()


@app.post("/api/run/end", response_model=CommandAccepted)
async def run_end(request: Request) -> CommandAccepted:
    b = brain_of(request)
    run_id = b.require_run()
    if b.link.connected:
        try:  # return home -> end_run -> fuse, through the FSM (doc 00 §6.11)
            b.fsm.come_back()
            b.db.event("run", f"Run {run_id}: ending, rover returning home")
            return b.accepted()
        except NotNow:
            pass
    # No rover link (or nothing to bring back): end where it stands.
    b.db.event("run", f"Run {run_id} ended")
    b.db.run_ended(run_id, "done")
    b.run_id = ""
    return b.accepted()


# --- missions ------------------------------------------------------------------------------------


async def _accept_mission(b: Brain, text: str, via: str) -> CommandAccepted:
    run_id = b.require_run()
    mission_id = b.ids.next("m")
    mission, source = await asyncio.to_thread(nlu.parse_mission_ex, text, run_id, None, mission_id)
    b.missions[mission_id] = mission
    b.db.mission(mission, "parsed")
    try:
        b.fsm.start_mission(mission, b.ids)
    except FsmBusy as e:
        b.db.mission(mission, "rejected")
        raise HTTPException(409, f"{mission_id} rejected: {e}") from e
    if source == "regex":
        b.db.event(
            "voice_offline", f"{mission_id} parsed offline (regex): Grok unreachable", "warn"
        )
    b.db.event(
        "mission",
        f"{mission_id} via {via} ({source}): radius {mission['radius_m']:g} m, k={mission['k']}, "
        f"{', '.join(mission['categories'])}",
        data={"source": source, "assumptions": mission["assumptions"]},
    )
    return b.accepted()


@app.post("/api/mission/text", response_model=CommandAccepted)
async def mission_text(req: MissionTextReq, request: Request) -> CommandAccepted:
    if not req.text.strip():
        raise HTTPException(422, "Empty mission text.")
    return await _accept_mission(brain_of(request), req.text.strip(), "text")


@app.post("/api/mission/voice", response_model=CommandAccepted)
async def mission_voice(request: Request) -> CommandAccepted:
    b = brain_of(request)
    b.require_run()
    audio = await request.body()
    if not audio:
        raise HTTPException(422, "Empty audio body.")
    mime = request.headers.get("content-type", "audio/webm")
    try:
        text, source = await asyncio.to_thread(asr.transcribe_ex, audio, mime)
    except asr.AsrUnavailable as e:
        b.db.event("voice_offline", f"Voice unavailable: {e}", "warn")
        raise HTTPException(503, f"Voice unavailable ({e}). Type the mission instead.") from e
    if source != "grok":
        b.db.event("voice_offline", f"Transcribed offline ({source})", "warn")
    b.db.event("voice", f'Heard: "{text}"', data={"source": source})
    if cmd := nlu.parse_quick(text):
        b.db.event("quick", f"Voice quick command '{cmd}'")
        return await _quick(b, cmd)
    return await _accept_mission(b, text, "voice")


# --- rover control --------------------------------------------------------------------------------


async def _quick(b: Brain, cmd: str) -> CommandAccepted:
    if cmd == "pause":
        b.fsm.pause()
    elif cmd == "resume":
        b.fsm.resume()
    elif cmd == "stop":
        await b.fsm.stop()
    elif cmd == "status":
        link = "up" if b.link.connected else "down"
        b.db.event("status", f"FSM {b.fsm.state}; {len(b.fsm.cands)} candidates; link {link}")
    else:
        try:
            b.fsm.skip() if cmd == "skip" else b.fsm.come_back()
        except NotNow as e:
            raise HTTPException(409, f"'{cmd}': {e}") from e
    return b.accepted()


@app.post("/api/quick", response_model=CommandAccepted)
async def quick(req: QuickReq, request: Request) -> CommandAccepted:
    return await _quick(brain_of(request), req.cmd)


@app.post("/api/estop", response_model=CommandAccepted)
async def estop(request: Request) -> CommandAccepted:
    b = brain_of(request)
    try:
        await b.fsm.estop()
    except (RoverError, OSError) as e:
        raise HTTPException(
            503, f"E-STOP could not reach the rover ({e}): use the physical kill switch!"
        ) from e
    return b.accepted()


@app.post("/api/estop/clear", response_model=CommandAccepted)
async def estop_clear(request: Request) -> CommandAccepted:
    b = brain_of(request)
    try:
        await b.fsm.clear_estop()
    except RoverError as e:
        raise HTTPException(503, f"clear E-STOP failed: {e}") from e
    return b.accepted()


@app.post("/api/approve", response_model=CommandAccepted)
async def approve(req: ApproveReq, request: Request) -> CommandAccepted:
    b = brain_of(request)
    try:
        b.fsm.approve(req.mission_id, req.approve)
    except NotNow as e:
        raise HTTPException(409, f"approve: {e}") from e
    b.db.event("approve", f"{req.mission_id} {'approved' if req.approve else 'rejected'}")
    return b.accepted()
