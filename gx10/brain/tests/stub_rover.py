"""Protocol-accurate stand-in for the Pi (doc 00 §6.2) for brain tests.

Not mocks/fake_rover.py (P2 owns that): this only exists so the brain can be tested
before it lands. Real WebSocket server; acks immediately, finishes ops after `op_delay_s`,
streams telemetry, latches E-STOP, counts heartbeats. Stations "see" the rocks in ROCKS
within SEEN_M, so a rock between two stations is reported twice (exercises dedupe).
Pass mapper_url to upload each station and sample to a real mapper and return its reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time

import httpx
from websockets.asyncio.server import ServerConnection, serve

from marsrover_shared.frames import wrap_deg
from marsrover_shared.schemas import (
    Ack,
    Cmd,
    Done,
    Err,
    Imu,
    MapperStationReply,
    Pose,
    SampleJson,
    SampleReply,
    ScoopState,
    StationJson,
    Telemetry,
)

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
    def __init__(
        self,
        op_delay_s: float = 0.01,
        telemetry_hz: float = 20.0,
        mapper_url: str | None = None,
    ) -> None:
        self.op_delay_s, self.telemetry_hz = op_delay_s, telemetry_hz
        self.mapper_url = mapper_url.rstrip("/") if mapper_url else None
        self.x = self.y = self.heading = 0.0
        self.run_id = ""
        self._last_cand = ""
        self._kf = 0
        self._http: httpx.AsyncClient | None = None
        self._jpg_bytes: bytes | None = None
        self._png_bytes: bytes | None = None
        self.estop = False
        self.heartbeats = 0
        self.ops: list[str] = []
        self.no_ack: set[str] = set()  # ops to swallow (tests the ack timeout)
        self.fail: dict[str, str] = {}  # op -> error code to reply with
        self.hold_station = asyncio.Event()  # cleared = station blocks (tests pause/stop)
        self.hold_station.set()
        self.hold_scoop = asyncio.Event()  # cleared = scoop blocks (tests skip/come_back)
        self.hold_scoop.set()
        self.lost: set[tuple[float, float]] = set()  # rock xy whose approach loses the target
        self.lose_first = False  # the first approached target is lost
        self._props = 0
        self._server = None
        self.url = ""

    async def __aenter__(self) -> StubRover:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/rover"
        if self.mapper_url:
            self._http = httpx.AsyncClient(base_url=self.mapper_url, timeout=30.0)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._http is not None:
            await self._http.aclose()
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
                    run_id=self.run_id,
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
        if cmd.op == "scoop":
            await self.hold_scoop.wait()
        if self.mapper_url and cmd.op in ("station", "scoop"):
            try:
                result = await self._publish(cmd, result)
            except Exception as e:
                await ws.send(
                    Err(cmd_id=cmd.cmd_id, code="E_UPLOAD", msg=str(e)[:300]).model_dump_json()
                )
                return
        await ws.send(Done(cmd_id=cmd.cmd_id, result=result).model_dump_json())

    def _apply(self, cmd: Cmd) -> dict:
        a = cmd.args
        if cmd.op == "start_run":
            self.run_id = str(a["run_id"])
            return {"ok": True}
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
            return self._station(
                a["station_id"], int(a.get("n_views", 8)), float(a.get("step_deg", 45))
            )
        elif cmd.op == "approach":
            self._last_cand = str(a.get("cand_id", ""))
            if self.lose_first:
                self.lose_first = False
                self.lost.add((round(a["x"], 3), round(a["y"], 3)))
            if (round(a["x"], 3), round(a["y"], 3)) in self.lost:
                return {"in_zone": False, "pose": self.pose().model_dump(), "offset_px": [99, 99]}
            dx, dy = a["x"] - self.x, a["y"] - self.y
            d = math.hypot(dx, dy)
            if d > 0.12:  # stop 12 cm short, facing the rock: scoop zone
                self.x += dx * (d - 0.12) / d
                self.y += dy * (d - 0.12) / d
            self.heading = math.degrees(math.atan2(dy, dx))
            return {"in_zone": True, "pose": self.pose().model_dump(), "offset_px": [0, 0]}
        elif cmd.op == "scoop":
            return {
                "present_image": f"http://stub/present/{a['sample_id']}.jpg",
                "uploaded": True,
                "pose": self.pose().model_dump(),
            }
        elif cmd.op == "snapshot":
            return {"url": "http://stub/snapshot.jpg"}
        elif cmd.op == "dump":
            return {"ok": True}
        return {"pose": self.pose().model_dump()}

    def _station(self, station_id: str, n_views: int = 8, step_deg: float = 45.0) -> dict:
        keyframes = []
        if self.mapper_url:
            # Absolute mission-frame heading at each capture (doc 00 §6.4).
            for i in range(n_views):
                self._kf += 1
                keyframes.append(
                    {
                        "kf_id": f"k_{self._kf:04d}",
                        "file": f"kf_{i:03d}.jpg",
                        "heading_deg": wrap_deg(self.heading + i * step_deg),
                        "t": time.time(),
                    }
                )
        proposals, crops = [], {}
        anchor = keyframes[0]["kf_id"] if keyframes else "k_0001"
        for rx, ry, size in ROCKS:
            if math.hypot(rx - self.x, ry - self.y) <= SEEN_M:
                self._props += 1
                pid = f"p_{self._props:04d}"
                proposals.append(
                    {
                        "prop_id": pid,
                        "kf_id": anchor,
                        "bbox_px": [10, 10, 20, 20],
                        "crop": f"crop_{pid}.jpg",
                        "xy": [rx, ry],
                        "size_m": size,
                        "contrast": 0.4,
                    }
                )
                crops[pid] = f"http://stub-mapper/crops/{pid}.jpg"
        return {
            "run_id": self.run_id,
            "station_id": station_id,
            "t": time.time(),
            "pose": self.pose().model_dump(),
            "keyframes": keyframes,
            "bev": {
                "file": "bev.png",
                "res_m_px": 0.005,
                "w_px": 360,
                "h_px": 360,
                "frame": "rover@station",
            },
            "proposals": proposals,
            "mapper": {
                "ok": True,
                "station_id": station_id,
                "stored": "/data",
                "urls": {"bev": "", "crops": crops},
            },
        }

    async def _publish(self, cmd: Cmd, result: dict) -> dict:
        """Upload the station or sample to the real mapper and use its reply (doc 00 §6.4–6.5)."""
        if cmd.op == "station":
            return await self._publish_station(result)
        return await self._publish_sample(str(cmd.args["sample_id"]), result)

    def _jpeg(self) -> bytes:
        if self._jpg_bytes is None:
            import io

            from PIL import Image

            buf = io.BytesIO()
            Image.new("RGB", (64, 48), (120, 72, 48)).save(buf, format="JPEG", quality=70)
            self._jpg_bytes = buf.getvalue()
        return self._jpg_bytes

    def _png(self) -> bytes:
        if self._png_bytes is None:
            import io

            from PIL import Image

            buf = io.BytesIO()
            Image.new("RGB", (64, 64), (122, 82, 52)).save(buf, format="PNG")
            self._png_bytes = buf.getvalue()
        return self._png_bytes

    async def _publish_station(self, result: dict) -> dict:
        meta = {k: v for k, v in result.items() if k != "mapper"}
        st = StationJson.model_validate(meta)
        body = st.model_dump(mode="json")
        jpg, png = self._jpeg(), self._png()
        files: list[tuple] = [
            ("station.json", ("station.json", json.dumps(body), "application/json"))
        ]
        files += [(kf.file, (kf.file, jpg, "image/jpeg")) for kf in st.keyframes]
        files.append((st.bev.file, (st.bev.file, png, "image/png")))
        files += [(p.crop, (p.crop, jpg, "image/jpeg")) for p in st.proposals]
        resp = await self._http.post("/ingest/station", files=files)
        if resp.status_code != 200:
            raise RuntimeError(f"ingest/station {resp.status_code}: {resp.text[:300]}")
        reply = MapperStationReply.model_validate(resp.json())
        return {**body, "mapper": reply.model_dump()}

    async def _publish_sample(self, sample_id: str, result: dict) -> dict:
        meta = SampleJson(
            run_id=self.run_id,
            sample_id=sample_id,
            cand_id=self._last_cand,
            t=time.time(),
            pose=self.pose(),
        )
        body = meta.model_dump(mode="json")
        files = [
            ("sample.json", ("sample.json", json.dumps(body), "application/json")),
            ("present.jpg", ("present.jpg", self._jpeg(), "image/jpeg")),
        ]
        resp = await self._http.post("/ingest/sample", files=files)
        if resp.status_code != 200:
            raise RuntimeError(f"ingest/sample {resp.status_code}: {resp.text[:300]}")
        reply = SampleReply.model_validate(resp.json())
        out = dict(result)
        out["present_image"] = reply.url
        out["uploaded"] = True
        return out


GOLDEN_ASSAY = {
    "assay_type": "bradford_sim",
    "simulated": True,
    "model_version": "bradford_sim-1",
    "seed": 3896889563,
    "a595": 0.2585,
    "concentration": 282.1,
    "units": "µg/mL",
    "verdict": "low",
    "in_linear_range": True,
    "standard_curve_params": {
        "standard": "BSA",
        "wavelength_nm": 595,
        "linear_range": [125.0, 1000.0],
        "true_model": {"type": "hyperbolic", "amax": 3.0, "k": 3000.0, "noise_sd": 0.01},
        "fit": {
            "type": "linear",
            "slope": 0.0007188,
            "intercept": 0.0557,
            "r2": 0.9947,
            "fit_range": [125.0, 1000.0],
        },
        "blank_a595": -0.0068,
        "standards": [{"c": 0, "a595": [0.0073, -0.0063, -0.001]}],
    },
}


class StubMapper:
    def __init__(self) -> None:
        self.fused: list[str] = []

    async def fuse(self, run_id: str) -> dict:
        self.fused.append(run_id)
        return {"ok": True, "tiles": 4}


class StubVision:
    """Deterministic /score: rocks further from home look more novel; same rock, same embedding.
    /analyze returns the golden assay; `empty[cand_id] = n` makes the first n scoops empty."""

    def __init__(self) -> None:
        self.calls = 0
        self.empty: dict[str, int] = {}
        self.empty_first = 0  # the first analyzed target comes up empty this many times
        self.analyzed: list[str] = []

    async def analyze(self, req):
        from marsrover_shared.schemas import AnalysisResult

        if not self.analyzed and self.empty_first:
            self.empty[req.cand_id] = self.empty_first
        self.analyzed.append(req.cand_id)
        if self.analyzed.count(req.cand_id) <= self.empty.get(req.cand_id, 0):
            return AnalysisResult(
                sample_id=req.sample_id,
                scoop_has_item=False,
                label="",
                confidence=0.0,
                caption="Empty scoop.",
                simulated=True,
            )
        return AnalysisResult(
            sample_id=req.sample_id,
            scoop_has_item=True,
            label="float_rock",
            confidence=0.62,
            caption="A small dark rock.",
            simulated=True,
            assay=GOLDEN_ASSAY,
        )

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
