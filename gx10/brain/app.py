"""Brain service, FastAPI :8000: the dashboard's only command API (doc 00 §6.11).

Built so far: runs, text and voice missions (Grok with the offline backup), health.
Still to come with the rover link and FSM: quick commands, E-STOP, approve, the campaign
itself. Those endpoints answer 503 until then, so the dashboard shows an honest error.

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
from brain.stdb_writer import BrainDb
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
NETWORK = yaml.safe_load((settings.ROOT / "config" / "network.yaml").read_text())


def _url(env_key: str, service: str) -> str:
    svc = NETWORK["services"][service]
    host = NETWORK["hosts"][svc["host"]]["ip"]
    return settings.env(env_key, f"http://{host}:{svc['port']}")


class Brain:
    """Process-wide state. One run at a time."""

    def __init__(self, db: BrainDb) -> None:
        self.db = db
        self.run_id = ""
        self.ids = IdGen()
        self.missions: dict[str, dict] = {}

    def accepted(self) -> CommandAccepted:
        return CommandAccepted(accepted=True, cmd_uid=self.ids.next("u"))

    def require_run(self) -> str:
        if not self.run_id:
            raise HTTPException(409, "No active run: start one first (POST /api/run/start).")
        return self.run_id


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.load_env()
    db = BrainDb(StdbClient.from_env("STDB_TOKEN_BRAIN"))
    app.state.brain = Brain(db)
    yield
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
        stdb, vision, mapper, overhead = await asyncio.gather(
            _probe(client, settings.env("STDB_URL", "http://192.168.50.10:3000") + "/v1/ping"),
            _probe(client, _url("VISION_URL", "vision") + "/health"),
            # UNVERIFIED: mapper /health pending the P3→P4 request; any HTTP reply counts as up.
            _probe(client, _url("MAPPER_URL", "mapper") + "/health", any_reply_ok=True),
            _probe(client, _url("OVERHEAD_URL", "overhead"), any_reply_ok=True),
        )
    return HealthResp(
        brain="ok", rover_link="down", vision=vision, mapper=mapper, stdb=stdb, overhead=overhead
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
    # Until the FSM exists this ends the run immediately (no return-home / fuse yet).
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
    if quick := nlu.parse_quick(text):
        # Quick commands need the rover link; record them so nothing is silently dropped.
        b.db.event(
            "quick", f"Voice quick command '{quick}' (not executed: no rover link yet)", "warn"
        )
        raise HTTPException(
            503, f"Heard quick command '{quick}', but the rover link isn't built yet."
        )
    return await _accept_mission(b, text, "voice")


# --- not built yet: need the rover link + FSM ----------------------------------------------

_NEEDS_ROVER = "Needs the rover link and FSM, which aren't built yet."


@app.post("/api/quick")
async def quick(req: QuickReq) -> None:
    raise HTTPException(503, f"quick '{req.cmd}': {_NEEDS_ROVER}")


@app.post("/api/estop")
async def estop() -> None:
    raise HTTPException(503, f"E-STOP: {_NEEDS_ROVER} Use the physical kill switch.")


@app.post("/api/estop/clear")
async def estop_clear() -> None:
    raise HTTPException(503, f"clear E-STOP: {_NEEDS_ROVER}")


@app.post("/api/approve")
async def approve(req: ApproveReq) -> None:
    raise HTTPException(503, f"approve {req.mission_id}: {_NEEDS_ROVER}")
