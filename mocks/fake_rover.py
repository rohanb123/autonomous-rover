"""fake_rover: a stand-in for the Raspberry Pi runtime, speaking the real WS protocol.

Serves the brain <-> rover WebSocket contract (doc 00 section 6.2) on ws://<host>:8765/rover and
the mapper uploads (doc 00 sections 6.4-6.5) with simulated motion, stations and scoops -- no
hardware, no camera, no SpacetimeDB. P4's brain and P3's mapper build against it from I0, so it
follows the contract exactly even where that is less realistic than the real Pi.

    python mocks/fake_rover.py                                # :8765, mapper on localhost:8200
    python mocks/fake_rover.py --mapper http://192.168.50.10:8200   # on marsnet
    python mocks/fake_rover.py --no-upload --speed-scale 20    # offline and fast (tests)
    python mocks/fake_rover.py --fail-rate 0.3 --seed 7        # exercise the brain's retries

Flags
    --host / --port    bind address and port (doc 00 section 3: 8765, path /rover)
    --mapper URL       base URL for the station/sample uploads
    --speed-scale N    run simulated time N times faster (drives, turns, stations, scoops)
    --noise SIGMA      Gaussian dead-reckoning slip and IMU noise (0 = perfectly repeatable)
    --fail-rate P      chance in [0, 1] that `approach`/`scoop` fail on purpose
    --seed N           RNG seed; the same seed replays the same stations and failures
    --no-upload        skip the HTTP POSTs and synthesise a successful mapper reply

Example client (every reply validates as a `marsrover_shared.schemas` model):

    import asyncio, json
    from websockets.asyncio.client import connect
    from marsrover_shared.schemas import Cmd, parse_rover_message

    async def main():
        async with connect("ws://localhost:8765/rover", ping_interval=None) as ws:
            await ws.send(Cmd(cmd_id="q_000001", op="start_run",
                              args={"run_id": "r_20261024_101500"}).model_dump_json())
            for _ in range(8):
                print(parse_rover_message(await ws.recv()))

    asyncio.run(main())

Differences from the real Pi, all deliberate: there is no :8080 snapshot server (`snapshot`
returns the documented URL without serving it), `turn` always reports `method:"gyro"`, and
`--no-upload` fabricates the mapper reply. Everything on the wire is the real contract.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
import math
import random
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "py"))  # works without an install (macOS .pth issue)

import httpx  # noqa: E402
import websockets  # noqa: E402
from PIL import Image, ImageChops, ImageDraw  # noqa: E402
from websockets.asyncio.server import ServerConnection, serve  # noqa: E402

from marsrover_shared.frames import mission_to_rover, rover_to_mission, wrap_deg  # noqa: E402
from marsrover_shared.ids import IdGen  # noqa: E402
from marsrover_shared.schemas import (  # noqa: E402
    Ack,
    Bev,
    Cmd,
    Done,
    Err,
    ErrCode,
    Event,
    Imu,
    MapperStationReply,
    Pose,
    Proposal,
    RoverState,
    SampleJson,
    SampleReply,
    ScoopPoseName,
    ScoopState,
    StationJson,
    StationKeyframe,
    StationResult,
    StationUrls,
    Telemetry,
)

log = logging.getLogger("fake_rover")

# --- contract constants (doc 00 section 4; these are the contract, don't tune them) --------

ACK_BUDGET_S = 0.5  # an ack must be on the wire within this
TELEMETRY_HZ = 5.0
WATCHDOG_HZ = 20.0
HEARTBEAT_TIMEOUT_S = 1.0  # no heartbeat for this long *while moving* -> stop the motors
BEV_RES_M_PX = 0.005
BEV_PX = 360
# Per-op done deadlines. Read at dispatch time so a test can shrink them.
OP_TIMEOUT_S: dict[str, float] = {"station": 60.0, "approach": 45.0, "scoop": 30.0}
# Doc 00 section 3: the real Pi serves this; fake_rover only hands the URL out.
SNAPSHOT_URL = "http://192.168.50.20:8080/snapshot.jpg"

# Ops answered on the reader loop, so they land even while a long op runs (P2 role doc 9).
IMMEDIATE_OPS = frozenset({"heartbeat", "estop", "stop", "clear_estop", "set_pose", "snapshot"})
# Ops that still work while the E-STOP latch is set; everything else gets E_ESTOP.
ESTOP_EXEMPT_OPS = frozenset({"heartbeat", "clear_estop", "set_pose", "snapshot"})
# States the heartbeat watchdog may cut the motors in. SCOOPING is left out: it is a servo
# sequence with one 4 cm nudge, not driving.
MOVING_STATES: frozenset[RoverState] = frozenset({"MOVING", "STATION", "APPROACH"})

# --- simulation constants (mock-only; contract fidelity beats realism) ---------------------

DRIVE_MPS = 0.30  # at speed = 1.0
TURN_DPS = 90.0  # at speed = 1.0
MIN_SPEED = 0.05  # `speed: 0` is legal in DriveArgs; crawl instead of dividing by zero
SIM_STEP_S = 0.05  # pose integration step, so 5 Hz telemetry shows motion
MIN_REAL_STEP_S = 0.02  # asyncio rounds sleeps up to ~16 ms on Windows; don't undershoot it
STATION_S = 5.0  # a whole station spin (P2 role doc 8), before --speed-scale
STATION_DEDUPE_M = 0.05  # proposals closer than this merge (P2 role doc 5.5)
SCOOP_STEP_S = 0.8  # per named scoop pose
SCOOP_PUSH_M = 0.04  # the slow nudge in DEPLOY (P2 role doc 4, sequence_pick)
SCOOP_STANDOFF_M = 0.10  # where `approach` parks: target this far ahead of the rover centre
SCOOP_ZONE_TOL_M = 0.05
APPROACH_SPEED = 0.4
UPLOAD_TIMEOUT_S = 10.0
RETRY_MIN_S, RETRY_MAX_S = 1.0, 30.0  # exponential backoff, capped (P2 role doc 4, uploader)

HOME_POSE = Pose(x=0.0, y=0.0, heading_deg=0.0, source="dr+gyro", sigma_xy_m=0.01)
# UNVERIFIED: plausible servo microseconds per named pose. The real values come from
# rover/calib/calib.yaml (scoop_us) once calibrate_scoop.py has run on the hardware.
SCOOP_US: dict[str, list[int]] = {
    "STOW": [1500, 1500],
    "DEPLOY": [1900, 1200],
    "CURL": [1700, 1800],
    "PRESENT": [1300, 1750],
    "DUMP": [1850, 1100],
}

# --- synthetic imagery ---------------------------------------------------------------------

KF_W, KF_H = 320, 240  # nav camera frame
CROP_PX = 96  # proposal crop (P2 role doc 5.2)
SAND_RGB = (196, 164, 132)
ROCK_RGB = (58, 48, 42)
PX_PER_M_AT_1M = 320.0  # UNVERIFIED crude pinhole: apparent size falls off with distance


@dataclass(frozen=True)
class Rock:
    """A simulated rock: rover-frame metres at the capturing view, plus its image box."""

    fwd_m: float
    lat_m: float
    size_m: float
    contrast: float
    bbox_px: tuple[int, int, int, int]


def _project(fwd_m: float, lat_m: float, size_m: float) -> tuple[int, int, int, int]:
    """Crude ground-plane projection of a rock into the nav-camera image. UNVERIFIED."""
    scale = PX_PER_M_AT_1M / max(fwd_m, 0.15)
    side = max(8, min(int(size_m * scale), KF_W // 3))
    u = int(KF_W / 2 - lat_m * scale)
    v = int(KF_H * 0.95 - (fwd_m - 0.15) * (KF_H * 0.45))
    x = max(0, min(u - side // 2, KF_W - side))
    y = max(0, min(v - side // 2, KF_H - side))
    return (x, y, side, side)


def _grain(rng: random.Random, w: int, h: int) -> Image.Image:
    """Sand-coloured regolith with cheap, seeded speckle (no numpy, no data files)."""
    sw, sh = max(1, w // 8), max(1, h // 8)
    raw = bytes(rng.randrange(256) for _ in range(sw * sh))
    noise = Image.frombytes("L", (sw, sh), raw).resize((w, h), Image.Resampling.BILINEAR)
    noise = noise.point(lambda v: int(v * 0.10))
    base = Image.new("RGB", (w, h), SAND_RGB)
    return ImageChops.add(base, Image.merge("RGB", (noise, noise, noise)))


def _shade(contrast: float) -> tuple[int, int, int]:
    v = int(40 + 60 * (1.0 - contrast))
    return (v, max(0, v - 8), max(0, v - 12))


def _jpeg(img: Image.Image, quality: int = 80) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def render_keyframe(rng: random.Random, rocks: list[Rock]) -> bytes:
    img = _grain(rng, KF_W, KF_H)
    draw = ImageDraw.Draw(img)
    for rock in rocks:
        x, y, w, h = rock.bbox_px
        draw.ellipse([x, y, x + w, y + h], fill=_shade(rock.contrast))
    return _jpeg(img)


def render_crop(rng: random.Random, rock: Rock) -> bytes:
    img = _grain(rng, CROP_PX, CROP_PX)
    pad = CROP_PX // 6
    ImageDraw.Draw(img).ellipse(
        [pad, pad, CROP_PX - pad, CROP_PX - pad], fill=_shade(rock.contrast)
    )
    return _jpeg(img)


def render_bev(rng: random.Random, rocks_rover: list[tuple[float, float, float]]) -> bytes:
    """A 360x360 rover-frame tile: image up = +x, image left = +y, 0.005 m/px (doc 00 4)."""
    img = _grain(rng, BEV_PX, BEV_PX)
    draw = ImageDraw.Draw(img)
    c = BEV_PX / 2
    for fwd_m, lat_m, size_m in rocks_rover:
        u, v = c - lat_m / BEV_RES_M_PX, c - fwd_m / BEV_RES_M_PX
        r = max(2.0, size_m / 2 / BEV_RES_M_PX)
        draw.ellipse([u - r, v - r, u + r, v + r], fill=ROCK_RGB)
    draw.ellipse([c - 3, c - 3, c + 3, c + 3], outline=(235, 235, 235))  # the rover itself
    return _png(img)


def render_present(rng: random.Random, sample_id: str) -> bytes:
    """The scoop held up to the camera: doc 00 section 6.5 present.jpg."""
    img = _grain(rng, KF_W, KF_H)
    draw = ImageDraw.Draw(img)
    draw.rectangle([60, 70, 260, 200], fill=(118, 118, 126))
    draw.ellipse([130, 110, 190, 165], fill=ROCK_RGB)
    draw.text((8, 8), f"PRESENT {sample_id} (simulated)", fill=(25, 25, 25))
    return _jpeg(img)


# --- state ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Options:
    """Everything the CLI can change. Frozen: the sim reads it, never rewrites it."""

    mapper: str = "http://localhost:8200"
    speed_scale: float = 1.0
    noise: float = 0.0
    fail_rate: float = 0.0
    seed: int = 0
    no_upload: bool = False


@dataclass
class SimState:
    """The rover's whole observable state. Pose/scoop are replaced, never mutated in place."""

    run_id: str = ""
    pose: Pose = field(default_factory=lambda: HOME_POSE)
    state: RoverState = "IDLE"
    estop: bool = False
    scoop: ScoopState = field(default_factory=lambda: ScoopState(pose="STOW", us=SCOOP_US["STOW"]))
    gz_dps: float = 0.0
    stop_flag: bool = False
    last_heartbeat: float = field(default_factory=time.monotonic)
    last_cand_id: str = ""  # doc 00 6.5 wants a cand_id that `scoop` args don't carry
    ids: IdGen = field(default_factory=IdGen)
    retry_queue: list[dict] = field(default_factory=list)


class OpError(Exception):
    """Raised by an op to answer with `err` instead of `done`."""

    def __init__(self, code: ErrCode, msg: str = "") -> None:
        super().__init__(f"{code}: {msg}" if msg else code)
        self.code: ErrCode = code
        self.msg = msg


@dataclass(frozen=True)
class _Prop:
    """One station proposal before it gets an id: which view saw it and where it really is."""

    kf_id: str
    heading_deg: float
    rock: Rock
    xy: tuple[float, float]  # mission frame


def _dedupe(props: list[_Prop]) -> list[_Prop]:
    """Merge station proposals closer than 5 cm, keeping the higher contrast (P2 role doc 5.5)."""
    keep: list[int] = []
    for i in sorted(range(len(props)), key=lambda i: -props[i].rock.contrast):
        if all(math.dist(props[i].xy, props[j].xy) > STATION_DEDUPE_M for j in keep):
            keep.append(i)
    return [props[i] for i in sorted(keep)]


# --- simulation ----------------------------------------------------------------------------


class Sim:
    """Simulated physics, imagery and uploads. Knows nothing about the WS protocol."""

    def __init__(self, opts: Options | None = None) -> None:
        self.opts = opts or Options()
        self.st = SimState()
        self.rng = random.Random(self.opts.seed)  # drives every reproducible decision
        self.sensors = random.Random(self.opts.seed + 1)  # telemetry jitter, off the hot path
        self.listeners: list[asyncio.Queue[Event]] = []
        self._running = False
        self._op: asyncio.Task | None = None
        self._abort: ErrCode | None = None
        self._retry: asyncio.Task | None = None

    # --- clock --------------------------------------------------------------------------

    async def sleep(self, seconds: float) -> None:
        """Simulated-time sleep: --speed-scale > 1 makes the mock run faster than real life."""
        await asyncio.sleep(seconds / self.opts.speed_scale)

    def _slices(self, total_s: float) -> list[float]:
        """Split simulated time into steps whose *real* sleeps the OS can actually honour."""
        if total_s <= 0:
            return []
        step = max(SIM_STEP_S, MIN_REAL_STEP_S * self.opts.speed_scale)
        n = max(1, math.ceil(total_s / step))
        return [total_s / n] * n

    # --- events -------------------------------------------------------------------------

    def emit(self, kind: str, detail: dict) -> None:
        ev = Event(kind=kind, detail=detail)
        for q in self.listeners:
            q.put_nowait(ev)

    # --- the one-op-at-a-time slot ------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._running

    def begin_op(self) -> None:
        """Claimed synchronously on dispatch, so a racing second op still gets E_BUSY."""
        self._running = True

    def set_op_task(self, task: asyncio.Task) -> None:
        self._op = task

    def end_op(self) -> None:
        self._running, self._op = False, None

    def abort(self, code: ErrCode) -> None:
        """Cut the running op short; it answers with `err code` instead of `done`."""
        if self._op is None or self._op.done():
            return  # nothing to cut: don't leave a reason behind for the next op
        self._abort = code
        self._op.cancel()

    def take_abort(self) -> ErrCode | None:
        code, self._abort = self._abort, None
        return code

    def close(self) -> None:
        """Drop the retry task (shutdown, tests). Queued bundles stay queued."""
        if self._retry is not None:
            self._retry.cancel()
            self._retry = None

    # --- telemetry ----------------------------------------------------------------------

    def _sensor_noise(self) -> float:
        return self.sensors.gauss(0.0, self.opts.noise) if self.opts.noise > 0 else 0.0

    def telemetry(self) -> Telemetry:
        st = self.st
        return Telemetry(
            run_id=st.run_id,
            pose=st.pose,
            state=st.state,
            estop=st.estop,
            imu=Imu(
                ax=self._sensor_noise(),
                ay=self._sensor_noise(),
                az=9.79 + self._sensor_noise(),
                gz_dps=st.gz_dps,
            ),
            scoop=st.scoop,
            cpu_temp_c=55.2,
            queue_len=1 if self._running else 0,
            uplink_backlog=len(st.retry_queue),
        )

    # --- motion primitives (no state/result handling: the ops above own that) -----------

    def _idle(self) -> None:
        self.st.gz_dps = 0.0
        if not self.st.estop:  # estop wins: never walk ESTOP back to IDLE
            self.st.state = "IDLE"

    def _translate(self, ds: float) -> None:
        p = self.st.pose
        h = math.radians(p.heading_deg)
        slip = self.rng.gauss(0.0, self.opts.noise) * abs(ds) if self.opts.noise > 0 else 0.0
        self.st.pose = p.model_copy(
            update={
                "x": p.x + ds * math.cos(h) - slip * math.sin(h),
                "y": p.y + ds * math.sin(h) + slip * math.cos(h),
            }
        )

    def _rotate(self, ddeg: float) -> None:
        p = self.st.pose
        drift = self.rng.gauss(0.0, self.opts.noise) * abs(ddeg) if self.opts.noise > 0 else 0.0
        self.st.pose = p.model_copy(update={"heading_deg": wrap_deg(p.heading_deg + ddeg + drift)})

    async def _advance(self, total_s: float, step: Callable[[float], None]) -> bool:
        """Run the sim forward in slices. False if `stop` cut it short."""
        for dt in self._slices(total_s):
            await self.sleep(dt)
            if self.st.stop_flag:
                return False
            step(dt)
        return True

    async def _drive_by(self, dist_m: float, speed: float) -> bool:
        v = DRIVE_MPS * max(speed, MIN_SPEED)
        sign = 1.0 if dist_m >= 0 else -1.0  # negative dist_m drives backwards
        return await self._advance(abs(dist_m) / v, lambda dt: self._translate(sign * v * dt))

    async def _turn_by(self, ddeg: float, speed: float) -> bool:
        w = TURN_DPS * max(speed, MIN_SPEED)
        sign = 1.0 if ddeg >= 0 else -1.0
        self.st.gz_dps = sign * w  # the gyro reads non-zero while turning
        try:
            return await self._advance(abs(ddeg) / w, lambda dt: self._rotate(sign * w * dt))
        finally:
            self.st.gz_dps = 0.0

    def _set_scoop(self, pose: ScoopPoseName) -> None:
        self.st.scoop = ScoopState(pose=pose, us=SCOOP_US[pose])

    def _maybe_fail(self, codes: tuple[ErrCode, ...]) -> ErrCode | None:
        """--fail-rate: pick an injected failure so the brain's retry paths get exercised."""
        if self.opts.fail_rate <= 0 or self.rng.random() >= self.opts.fail_rate:
            return None
        return codes[self.rng.randrange(len(codes))]

    # --- ops (doc 00 section 6.2) -------------------------------------------------------

    def start_run(self, run_id: str) -> dict:
        self.st.run_id = run_id
        self.st.ids = IdGen()  # k_/p_ counters are per run (doc 00 section 5)
        self.st.pose = HOME_POSE  # a run starts on the home pad, which defines the frame
        return {"ok": True}

    def set_pose(self, x: float, y: float, heading_deg: float, source: str) -> dict:
        self.st.pose = Pose(x=x, y=y, heading_deg=wrap_deg(heading_deg), source=source)
        return {"pose": self.st.pose.model_dump()}

    def stop(self) -> dict:
        self.st.stop_flag = True
        self.st.gz_dps = 0.0
        if not self._running:
            self._idle()
        return {"pose": self.st.pose.model_dump()}

    def estop(self) -> dict:
        self.st.estop = True
        self.st.state = "ESTOP"
        self.st.stop_flag = True
        self.abort("E_ESTOP")  # works mid-op: P2 role doc section 9
        return {"latched": True}

    def clear_estop(self) -> dict:
        self.st.estop = False
        self.st.stop_flag = False
        self.st.state = "IDLE"
        return {"latched": False}

    def snapshot(self) -> dict:
        return {"url": f"{SNAPSHOT_URL}?t={time.time():.3f}"}

    async def drive(self, dist_m: float, speed: float) -> dict:
        self.st.stop_flag = False
        self.st.state = "MOVING"
        try:
            await self._drive_by(dist_m, speed)
        finally:
            self._idle()
        return {"pose": self.st.pose.model_dump()}

    async def turn(self, deg: float, speed: float) -> dict:
        self.st.stop_flag = False
        self.st.state = "MOVING"
        try:
            await self._turn_by(deg, speed)
        finally:
            self._idle()
        # UNVERIFIED: the mock always claims a gyro-closed turn. The real Pi reports "timed"
        # when imu.has_gyro() is false (P2 role doc section 4, motion.turn).
        return {"pose": self.st.pose.model_dump(), "method": "gyro"}

    async def scoop_pose(self, pose: ScoopPoseName) -> dict:
        self.st.state = "SCOOPING"
        try:
            self._set_scoop(pose)
            await self.sleep(SCOOP_STEP_S)
        finally:
            self._idle()
        return {"ok": True}

    async def dump(self) -> dict:
        self.st.state = "SCOOPING"
        try:
            for pose in ("DUMP", "STOW"):
                self._set_scoop(pose)
                await self.sleep(SCOOP_STEP_S)
        finally:
            self._idle()
        return {"ok": True}

    async def approach(self, cand_id: str, x: float, y: float) -> dict:
        st = self.st
        st.stop_flag = False
        st.state = "APPROACH"
        st.last_cand_id = cand_id  # `scoop` needs it for sample.json (doc 00 section 6.5)
        failure = self._maybe_fail(("E_LOST_TARGET",))
        try:
            bearing = math.degrees(math.atan2(y - st.pose.y, x - st.pose.x))
            await self._turn_by(wrap_deg(bearing - st.pose.heading_deg), APPROACH_SPEED)
            if failure is not None:
                raise OpError(failure, f"lost {cand_id} while closing in")
            gap = math.dist((st.pose.x, st.pose.y), (x, y)) - SCOOP_STANDOFF_M
            await self._drive_by(gap, APPROACH_SPEED)
            # Residual in the scoop camera's frame: image +x is rover -y, image +y is rover -x.
            rx, ry = mission_to_rover(st.pose, (x, y))
            offset_px = [round(-ry / BEV_RES_M_PX), round(-(rx - SCOOP_STANDOFF_M) / BEV_RES_M_PX)]
            in_zone = math.dist((rx, ry), (SCOOP_STANDOFF_M, 0.0)) <= SCOOP_ZONE_TOL_M
        finally:
            self._idle()
        return {"in_zone": in_zone, "pose": st.pose.model_dump(), "offset_px": offset_px}

    async def scoop(self, sample_id: str) -> dict:
        st = self.st
        st.stop_flag = False
        st.state = "SCOOPING"
        failure = self._maybe_fail(("E_SCOOP", "E_UPLOAD"))
        try:
            self._set_scoop("DEPLOY")
            await self._drive_by(SCOOP_PUSH_M, 0.2)  # P2 role doc 4: nudge forward, slowly
            await self.sleep(SCOOP_STEP_S)
            for pose in ("CURL", "PRESENT"):
                self._set_scoop(pose)
                await self.sleep(SCOOP_STEP_S)
            if failure == "E_SCOOP":
                raise OpError("E_SCOOP", f"scoop came up empty for {sample_id}")
            if failure == "E_UPLOAD":
                raise OpError("E_UPLOAD", f"could not hand {sample_id} to the mapper")
            sample = SampleJson(
                run_id=st.run_id,
                sample_id=sample_id,
                cand_id=st.last_cand_id,
                t=time.time(),
                pose=st.pose,
            )
            url, uploaded = await self.upload_sample(sample, render_present(self.rng, sample_id))
        finally:
            self._idle()
        return {"present_image": url, "uploaded": uploaded, "pose": st.pose.model_dump()}

    async def station(self, station_id: str, n_views: int, step_deg: float) -> dict:
        """Spin, capture a keyframe per heading, propose rocks, stitch a BEV tile, upload."""
        st = self.st
        st.stop_flag = False
        st.state = "STATION"
        self._set_scoop("STOW")  # or the scoop is in every photo (P2 role doc section 9)
        base = st.pose  # the station's position; only the heading changes while spinning
        start_heading = base.heading_deg
        t0 = time.time()
        per_view = STATION_S / (n_views + 1)
        keyframes: list[StationKeyframe] = []
        seen: list[_Prop] = []
        views: dict[str, bytes] = {}
        try:
            for i in range(n_views):
                heading = wrap_deg(start_heading + i * step_deg)
                st.pose = base.model_copy(update={"heading_deg": heading})
                kf_id = st.ids.next("k")
                name = f"kf_{i:03d}.jpg"
                rocks = [self._spawn_rock() for _ in range(self.rng.randint(1, 3))]
                views[name] = render_keyframe(self.rng, rocks)
                # doc 00 6.4 (contract 1.3.0): heading_deg is the ABSOLUTE mission heading.
                keyframes.append(
                    StationKeyframe(kf_id=kf_id, file=name, heading_deg=heading, t=time.time())
                )
                seen += [
                    _Prop(kf_id, heading, r, rover_to_mission(st.pose, (r.fwd_m, r.lat_m)))
                    for r in rocks
                ]
                rate = step_deg / per_view
                st.gz_dps = rate  # the gyro reads the spin, not zero, between views
                if not await self._advance(per_view, lambda dt, w=rate: self._rotate(w * dt)):
                    break
            st.gz_dps = 0.0
            st.pose = base.model_copy(update={"heading_deg": start_heading})  # and back
            await self._advance(per_view, lambda _dt: None)
        finally:
            self._idle()
        station, files = self._bundle(station_id, t0, base, keyframes, views, _dedupe(seen))
        reply = await self.upload_station(station, files)
        bundle = {**station.model_dump(), "mapper": reply.model_dump()}
        return StationResult.model_validate(bundle).model_dump()

    def _spawn_rock(self) -> Rock:
        fwd = self.rng.uniform(0.30, 1.10)
        lat = self.rng.uniform(-0.35, 0.35)
        size = round(self.rng.uniform(0.012, 0.045), 4)
        contrast = round(self.rng.uniform(0.20, 0.80), 2)
        return Rock(fwd, lat, size, contrast, _project(fwd, lat, size))

    def _bundle(
        self,
        station_id: str,
        t0: float,
        base: Pose,
        keyframes: list[StationKeyframe],
        views: dict[str, bytes],
        props: list[_Prop],
    ) -> tuple[StationJson, dict[str, bytes]]:
        """Mint p_ ids, render the crops and the BEV tile, assemble station.json (doc 00 6.4)."""
        st = self.st
        files = dict(views)
        proposals: list[Proposal] = []
        bev_rocks: list[tuple[float, float, float]] = []
        for p in props:
            prop_id = st.ids.next("p")
            crop = f"crop_{prop_id}.jpg"
            files[crop] = render_crop(self.rng, p.rock)
            proposals.append(
                Proposal(
                    prop_id=prop_id,
                    kf_id=p.kf_id,
                    bbox_px=list(p.rock.bbox_px),
                    crop=crop,
                    xy=p.xy,
                    size_m=p.rock.size_m,
                    contrast=p.rock.contrast,
                )
            )
            # The tile is the rover frame at the station's start heading, not at capture.
            bev_rocks.append((*mission_to_rover(base, p.xy), p.rock.size_m))
        files["bev.png"] = render_bev(self.rng, bev_rocks)
        station = StationJson(
            run_id=st.run_id,
            station_id=station_id,
            t=t0,
            pose=base,
            keyframes=keyframes,
            bev=Bev(
                file="bev.png",
                res_m_px=BEV_RES_M_PX,
                w_px=BEV_PX,
                h_px=BEV_PX,
                frame="rover@station",
            ),
            proposals=proposals,
        )
        return station, files

    # --- uploads (doc 00 sections 6.4-6.5) ----------------------------------------------

    async def upload_station(
        self, station: StationJson, files: dict[str, bytes]
    ) -> MapperStationReply:
        if self.opts.no_upload:
            return self._offline_reply(station)
        try:
            return await self._post_station(station, files)
        except (httpx.HTTPError, ValueError) as e:
            self._queue_retry({"kind": "station", "station": station, "files": files})
            self._upload_failed("/ingest/station", station.station_id, e)
            # Doc 00 6.4 / P2 role doc 5.6: answer `done` anyway, with mapper.ok false.
            return MapperStationReply(
                ok=False, station_id=station.station_id, stored="", urls=StationUrls(bev="")
            )

    async def upload_sample(self, sample: SampleJson, jpg: bytes) -> tuple[str, bool]:
        """Returns (present_image, uploaded) for the `scoop` result."""
        local = f"runs/{sample.run_id}/samples/{sample.sample_id}/present.jpg"
        if self.opts.no_upload:
            return f"{self.opts.mapper}/files/{local}", True
        try:
            return (await self._post_sample(sample, jpg)).url, True
        except (httpx.HTTPError, ValueError) as e:
            self._queue_retry({"kind": "sample", "sample": sample, "jpg": jpg})
            self._upload_failed("/ingest/sample", sample.sample_id, e)
            return local, False  # the bundle is still on the SD card; the retry loop has it

    def _offline_reply(self, station: StationJson) -> MapperStationReply:
        """--no-upload: pretend the mapper stored it, so the happy path works with no mapper."""
        rel = f"runs/{station.run_id}/stations/{station.station_id}"
        base = f"{self.opts.mapper}/files/{rel}"
        return MapperStationReply(
            ok=True,
            station_id=station.station_id,
            stored=f"/data/{rel}/",
            urls=StationUrls(
                bev=f"{base}/{station.bev.file}",
                crops={p.prop_id: f"{base}/{p.crop}" for p in station.proposals},
            ),
        )

    def _upload_failed(self, endpoint: str, ref: str, exc: Exception) -> None:
        log.warning("upload to %s failed for %s: %s", endpoint, ref, exc)
        self.emit(
            "upload_retry",
            {
                "endpoint": endpoint,
                "ref": ref,
                "error": str(exc) or type(exc).__name__,
                "backlog": len(self.st.retry_queue),
            },
        )

    async def _post_station(
        self, station: StationJson, files: dict[str, bytes]
    ) -> MapperStationReply:
        parts = _multipart("station.json", station.model_dump_json(), files)
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT_S) as client:
            r = await client.post(f"{self.opts.mapper}/ingest/station", files=parts)
            r.raise_for_status()
            return MapperStationReply.model_validate_json(r.content)

    async def _post_sample(self, sample: SampleJson, jpg: bytes) -> SampleReply:
        parts = _multipart("sample.json", sample.model_dump_json(), {"present.jpg": jpg})
        async with httpx.AsyncClient(timeout=UPLOAD_TIMEOUT_S) as client:
            r = await client.post(f"{self.opts.mapper}/ingest/sample", files=parts)
            r.raise_for_status()
            return SampleReply.model_validate_json(r.content)

    def _queue_retry(self, item: dict) -> None:
        self.st.retry_queue.append(item)  # shows up as telemetry.uplink_backlog
        if self._retry is None or self._retry.done():
            self._retry = asyncio.create_task(self._retry_loop(), name="upload-retry")

    async def _retry_loop(self) -> None:
        """Exponential backoff, capped at 30 s (P2 role doc section 4, Uploader.retry_loop)."""
        backoff = RETRY_MIN_S
        while self.st.retry_queue:
            await asyncio.sleep(backoff)
            item = self.st.retry_queue[0]
            try:
                if item["kind"] == "station":
                    await self._post_station(item["station"], item["files"])
                else:
                    await self._post_sample(item["sample"], item["jpg"])
            except (httpx.HTTPError, ValueError) as e:
                backoff = min(backoff * 2, RETRY_MAX_S)
                log.debug("retry failed, next in %.0f s: %s", backoff, e)
                continue
            self.st.retry_queue.pop(0)
            backoff = RETRY_MIN_S


def _multipart(meta_name: str, meta_json: str, files: dict[str, bytes]) -> list[tuple]:
    """Doc 00 6.4-6.5: one part per file, each part named after the file itself."""
    parts: list[tuple] = [(meta_name, (meta_name, meta_json.encode(), "application/json"))]
    for name, data in sorted(files.items()):
        kind = "image/png" if name.endswith(".png") else "image/jpeg"
        parts.append((name, (name, data, kind)))
    return parts


# --- WebSocket session ---------------------------------------------------------------------


def _cmd_id_of(raw: str | bytes) -> str | None:
    """Best-effort cmd_id out of a frame that failed validation, so we can answer it."""
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        value = json.loads(raw).get("cmd_id")
        if isinstance(value, str) and value:
            return value
    return None


def _first_error(exc: ValueError) -> str:
    """One short line out of a pydantic ValidationError, for err.msg."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        for e in errors():
            loc = ".".join(str(p) for p in e.get("loc", ()))
            return f"{loc}: {e.get('msg', '')}".strip(": ")
    return str(exc).splitlines()[0]


class Session:
    """One brain connection: reader + ack/dispatch, 5 Hz telemetry, 20 Hz watchdog."""

    def __init__(self, ws: ServerConnection, sim: Sim) -> None:
        self.ws = ws
        self.sim = sim
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self._cmds: set[asyncio.Task] = set()

    async def run(self) -> None:
        self.sim.st.last_heartbeat = time.monotonic()
        self.sim.listeners.append(self.events)
        loops = [
            asyncio.create_task(self._telemetry_loop(), name="telemetry"),
            asyncio.create_task(self._watchdog_loop(), name="watchdog"),
            asyncio.create_task(self._event_loop(), name="events"),
        ]
        try:
            async for raw in self.ws:
                await self._on_frame(raw)
        finally:
            with contextlib.suppress(ValueError):
                self.sim.listeners.remove(self.events)
            self.sim.stop()  # brain gone -> motors stop, same as the watchdog would
            for t in (*loops, *self._cmds):
                t.cancel()
            await asyncio.gather(*loops, *self._cmds, return_exceptions=True)

    # --- outgoing -----------------------------------------------------------------------

    async def send(self, msg: Ack | Done | Err | Telemetry | Event) -> None:
        with contextlib.suppress(websockets.WebSocketException):
            await self.ws.send(msg.model_dump_json())

    async def _telemetry_loop(self) -> None:
        while True:
            await self.send(self.sim.telemetry())
            await asyncio.sleep(1.0 / TELEMETRY_HZ)

    async def _event_loop(self) -> None:
        while True:
            await self.send(await self.events.get())

    async def _watchdog_loop(self) -> None:
        """Doc 00 section 4: stop the motors if no heartbeat for 1.0 s *while moving*."""
        while True:
            await asyncio.sleep(1.0 / WATCHDOG_HZ)
            sim = self.sim
            if time.monotonic() - sim.st.last_heartbeat <= HEARTBEAT_TIMEOUT_S:
                continue
            if sim.busy and sim.st.state in MOVING_STATES:
                log.warning("heartbeat watchdog: stopping (state=%s)", sim.st.state)
                sim.emit("stall", {"reason": "heartbeat_watchdog", "state": sim.st.state})
                # Doc 00 doesn't name a code for this; E_TIMEOUT tells the brain the motion
                # did not finish, rather than letting it believe a `done` it never earned.
                sim.abort("E_TIMEOUT")

    # --- incoming -----------------------------------------------------------------------

    async def _on_frame(self, raw: str | bytes) -> None:
        try:
            cmd = Cmd.model_validate_json(raw)
        except ValueError as e:
            cmd_id = _cmd_id_of(raw)
            log.info("bad cmd (%s): %s", cmd_id, e)
            if cmd_id:  # no cmd_id -> nothing to answer, so drop it
                await self.send(Err(cmd_id=cmd_id, code="E_BAD_ARGS", msg=_first_error(e)))
            return
        if cmd.op == "heartbeat":
            self.sim.st.last_heartbeat = time.monotonic()  # no ack, no done (doc 00 6.2)
            return
        await self.send(Ack(cmd_id=cmd.cmd_id))
        await self._dispatch(cmd)

    async def _dispatch(self, cmd: Cmd) -> None:
        sim = self.sim
        if sim.st.estop and cmd.op not in ESTOP_EXEMPT_OPS:
            await self.send(Err(cmd_id=cmd.cmd_id, code="E_ESTOP", msg="E-STOP latched"))
            return
        if cmd.op in IMMEDIATE_OPS:
            try:
                await self.send(Done(cmd_id=cmd.cmd_id, result=self._immediate(cmd)))
            except OpError as e:
                await self.send(Err(cmd_id=cmd.cmd_id, code=e.code, msg=e.msg))
            return
        if sim.busy:
            await self.send(Err(cmd_id=cmd.cmd_id, code="E_BUSY", msg=f"busy, {cmd.op} refused"))
            return
        sim.begin_op()  # synchronous, so the next frame already sees us busy
        task = asyncio.create_task(self._run_long(cmd), name=f"cmd:{cmd.cmd_id}")
        self._cmds.add(task)
        task.add_done_callback(self._cmds.discard)

    def _immediate(self, cmd: Cmd) -> dict:
        sim = self.sim
        if cmd.op == "estop":
            return sim.estop()
        if cmd.op == "clear_estop":
            return sim.clear_estop()
        if cmd.op == "stop":
            return sim.stop()
        if cmd.op == "set_pose":
            return sim.set_pose(**cmd.args)
        if cmd.op == "snapshot":
            return sim.snapshot()
        raise OpError("E_INTERNAL", f"{cmd.op} is not an immediate op")

    async def _long(self, cmd: Cmd) -> dict:
        sim = self.sim
        if cmd.op == "start_run":
            return sim.start_run(**cmd.args)
        if cmd.op == "drive":
            return await sim.drive(**cmd.args)
        if cmd.op == "turn":
            return await sim.turn(**cmd.args)
        if cmd.op == "station":
            return await sim.station(**cmd.args)
        if cmd.op == "approach":
            return await sim.approach(**cmd.args)
        if cmd.op == "scoop":
            return await sim.scoop(**cmd.args)
        if cmd.op == "dump":
            return await sim.dump()
        if cmd.op == "scoop_pose":
            return await sim.scoop_pose(**cmd.args)
        raise OpError("E_INTERNAL", f"{cmd.op} has no handler")

    async def _run_long(self, cmd: Cmd) -> None:
        """Hold the one-op slot, enforce the op's deadline, answer exactly once."""
        sim = self.sim
        if sim.st.estop:  # closes the race between `estop` and a just-dispatched op
            sim.end_op()
            await self.send(Err(cmd_id=cmd.cmd_id, code="E_ESTOP", msg="E-STOP latched"))
            return
        limit = OP_TIMEOUT_S.get(cmd.op)
        inner = asyncio.create_task(self._long(cmd), name=f"op:{cmd.op}")
        sim.set_op_task(inner)
        try:
            await self.send(Done(cmd_id=cmd.cmd_id, result=await asyncio.wait_for(inner, limit)))
        except TimeoutError:
            await self.send(
                Err(cmd_id=cmd.cmd_id, code="E_TIMEOUT", msg=f"{cmd.op} exceeded {limit:g} s")
            )
        except OpError as e:
            await self.send(Err(cmd_id=cmd.cmd_id, code=e.code, msg=e.msg))
        except asyncio.CancelledError:
            code = sim.take_abort()
            if code is None:  # a real shutdown, not an abort
                raise
            await self.send(Err(cmd_id=cmd.cmd_id, code=code, msg=f"{cmd.op} aborted"))
        except Exception as e:  # a bug in the mock, not a contract case
            log.exception("%s failed", cmd.op)
            await self.send(Err(cmd_id=cmd.cmd_id, code="E_INTERNAL", msg=repr(e)))
        finally:
            sim.end_op()


# --- server --------------------------------------------------------------------------------


def make_handler(sim: Sim) -> Callable:
    async def handler(ws: ServerConnection) -> None:
        path = ws.request.path.split("?")[0] if ws.request is not None else "/rover"
        if path != "/rover":  # doc 00 section 3: the endpoint is /rover
            await ws.close(code=1008, reason="expected /rover")
            return
        log.info("brain connected: %s", ws.remote_address)
        try:
            await Session(ws, sim).run()
        except websockets.WebSocketException as e:
            log.debug("connection closed: %s", e)
        finally:
            log.info("brain disconnected")

    return handler


def serve_rover(sim: Sim, host: str, port: int):
    """`async with serve_rover(...) as server:` -- port 0 picks a free port (tests)."""
    return serve(make_handler(sim), host, port, ping_interval=None)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    p.add_argument("--port", type=int, default=8765, help="WS port (doc 00 section 3: 8765)")
    p.add_argument(
        "--mapper",
        default="http://localhost:8200",
        help="mapper base URL; on marsnet use http://192.168.50.10:8200",
    )
    p.add_argument(
        "--speed-scale", type=float, default=1.0, help="run simulated time N times faster"
    )
    p.add_argument("--noise", type=float, default=0.0, help="Gaussian pose/IMU noise sigma")
    p.add_argument("--fail-rate", type=float, default=0.0, help="0-1 chance an op fails on purpose")
    p.add_argument("--seed", type=int, default=0, help="RNG seed; same seed -> same run")
    p.add_argument("--no-upload", action="store_true", help="skip mapper uploads; fake a reply")
    return p.parse_args(argv)


def options_from(args: argparse.Namespace) -> Options:
    return Options(
        mapper=args.mapper,
        speed_scale=args.speed_scale,
        noise=args.noise,
        fail_rate=args.fail_rate,
        seed=args.seed,
        no_upload=args.no_upload,
    )


async def main_async(args: argparse.Namespace) -> None:
    sim = Sim(options_from(args))
    async with serve_rover(sim, args.host, args.port) as server:
        port = server.sockets[0].getsockname()[1] if server.sockets else args.port
        log.info("fake_rover on ws://%s:%d/rover (mapper %s)", args.host, port, args.mapper)
        await asyncio.get_running_loop().create_future()  # serve until cancelled


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main_async(parse_args(argv)))


if __name__ == "__main__":
    main()
