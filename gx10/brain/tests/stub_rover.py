"""Protocol-accurate stand-in for the Pi (doc 00 §6.2) for brain tests.

Not mocks/fake_rover.py (P2 owns that): this only exists so the brain can be tested
before it lands. Real WebSocket server; acks immediately, finishes ops after `op_delay_s`,
streams telemetry, latches E-STOP, counts heartbeats. Stations "see" the rocks in ROCKS
within SEEN_M, so a rock between two stations is reported twice (exercises dedupe).
"""

from __future__ import annotations

import asyncio
import contextlib
import math

from websockets.asyncio.server import ServerConnection, serve

from marsrover_shared.frames import wrap_deg
from marsrover_shared.schemas import Ack, Cmd, Done, Err, Imu, Pose, ScoopState, Telemetry

# (x, y, size_m) in the mission frame. r3 sits between home and the +x station.
ROCKS = [
    (0.5, 0.2, 0.02),
    (-0.4, 0.5, 0.025),
    (0.45, -0.1, 0.03),
    (1.3, 0.1, 0.018),
    (-0.2, -1.1, 0.04),
]
SEEN_M = 0.85


class StubRover:
    def __init__(self, op_delay_s: float = 0.01, telemetry_hz: float = 20.0) -> None:
        self.op_delay_s, self.telemetry_hz = op_delay_s, telemetry_hz
        self.x = self.y = self.heading = 0.0
        self.estop = False
        self.heartbeats = 0
        self.ops: list[str] = []
        self.no_ack: set[str] = set()  # ops to swallow (tests the ack timeout)
        self.fail: dict[str, str] = {}  # op -> error code to reply with
        self.hold_station = asyncio.Event()  # cleared = station blocks (tests pause/stop)
        self.hold_station.set()
        self._props = 0
        self._server = None
        self.url = ""

    async def __aenter__(self) -> StubRover:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/rover"
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        await self._server.wait_closed()

    def pose(self) -> Pose:
        return Pose(x=self.x, y=self.y, heading_deg=self.heading, source="dr+gyro", sigma_xy_m=0.02)

    async def _handle(self, ws: ServerConnection) -> None:
        tel = asyncio.create_task(self._telemetry(ws))
        try:
            async for raw in ws:
                cmd = Cmd.model_validate_json(raw)
                if cmd.op == "heartbeat":
                    self.heartbeats += 1
                    continue
                asyncio.create_task(self._run(ws, cmd))
        finally:
            tel.cancel()

    async def _telemetry(self, ws: ServerConnection) -> None:
        with contextlib.suppress(Exception):
            while True:
                t = Telemetry(
                    run_id="",
                    pose=self.pose(),
                    state="ESTOP" if self.estop else "IDLE",
                    estop=self.estop,
                    imu=Imu(ax=0, ay=0, az=9.8, gz_dps=0),
                    scoop=ScoopState(pose="STOW", us=[1500, 1500]),
                    cpu_temp_c=50.0,
                    queue_len=0,
                    uplink_backlog=0,
                )
                await ws.send(t.model_dump_json())
                await asyncio.sleep(1 / self.telemetry_hz)

    async def _run(self, ws: ServerConnection, cmd: Cmd) -> None:
        self.ops.append(cmd.op)
        if cmd.op in self.no_ack:
            return
        if cmd.op == "estop":
            self.estop = True
            await ws.send(Ack(cmd_id=cmd.cmd_id).model_dump_json())
            await ws.send(Done(cmd_id=cmd.cmd_id, result={"latched": True}).model_dump_json())
            return
        if self.estop and cmd.op != "clear_estop":
            await ws.send(Err(cmd_id=cmd.cmd_id, code="E_ESTOP", msg="latched").model_dump_json())
            return
        await ws.send(Ack(cmd_id=cmd.cmd_id).model_dump_json())
        if code := self.fail.get(cmd.op):
            await ws.send(Err(cmd_id=cmd.cmd_id, code=code, msg="injected").model_dump_json())
            return
        await asyncio.sleep(self.op_delay_s)
        result = self._apply(cmd)
        if cmd.op == "station":
            await self.hold_station.wait()
        await ws.send(Done(cmd_id=cmd.cmd_id, result=result).model_dump_json())

    def _apply(self, cmd: Cmd) -> dict:
        a = cmd.args
        if cmd.op == "turn":
            self.heading = wrap_deg(self.heading + a["deg"])
        elif cmd.op == "drive":
            h = math.radians(self.heading)
            self.x += a["dist_m"] * math.cos(h)
            self.y += a["dist_m"] * math.sin(h)
        elif cmd.op == "clear_estop":
            self.estop = False
            return {"latched": False}
        elif cmd.op == "station":
            return self._station(a["station_id"])
        return {"pose": self.pose().model_dump()}

    def _station(self, station_id: str) -> dict:
        proposals, crops = [], {}
        for rx, ry, size in ROCKS:
            if math.hypot(rx - self.x, ry - self.y) <= SEEN_M:
                self._props += 1
                pid = f"p_{self._props:04d}"
                proposals.append(
                    {
                        "prop_id": pid,
                        "kf_id": "k_0001",
                        "bbox_px": [10, 10, 20, 20],
                        "crop": f"crop_{pid}.jpg",
                        "xy": [rx, ry],
                        "size_m": size,
                        "contrast": 0.4,
                    }
                )
                crops[pid] = f"http://stub-mapper/crops/{pid}.jpg"
        return {
            "run_id": "r_test",
            "station_id": station_id,
            "t": 0.0,
            "pose": self.pose().model_dump(),
            "keyframes": [],
            "bev": {"file": "bev.png", "res_m_px": 0.005, "w_px": 360, "h_px": 360},
            "proposals": proposals,
            "mapper": {
                "ok": True,
                "station_id": station_id,
                "stored": "/data",
                "urls": {"bev": "", "crops": crops},
            },
        }


class StubVision:
    """Deterministic /score: rocks further from home look more novel; same rock, same embedding."""

    def __init__(self) -> None:
        self.calls = 0

    async def score(self, run_id, items):
        from marsrover_shared.schemas import ScoreRespItem

        self.calls += 1
        out = []
        for it in items:
            x, y = it.xy
            out.append(
                ScoreRespItem(
                    prop_id=it.prop_id,
                    visual=0.5,
                    novelty=min(1.0, 0.3 + 0.4 * math.hypot(x, y)),
                    pickable=1.0,
                    category="float_rock",
                    category_probs={"float_rock": 0.7, "pebble": 0.3},
                    terrain="soil",
                    embedding=[round(x, 2), round(y, 2), 1.0],
                    model="stub",
                )
            )
        return out
