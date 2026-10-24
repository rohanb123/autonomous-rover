"""fake_brain: a scripted ~3-minute run written to SpacetimeDB through the real reducers.

Stands in for the whole backend (brain + Pi + vision + mapper) so the dashboard can be
built without hardware (docs/04 §1, P4 role doc §8). One fake arena drives every image,
so local tiles, keyframes, pins and the fused global map all line up.

    python mocks/fake_brain.py              # real time (~2.5 min)
    python mocks/fake_brain.py --speed 20   # same rows, 20x faster

Needs config/.env (STDB_URL, STDB_DB, STDB_TOKEN_BRAIN) and a published module
(./scripts/publish_stdb.sh). Rows are stamped with wall-clock time as they're sent.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared" / "py"))  # works without an install (macOS .pth issue)

from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

from marsrover_shared import eventlog  # noqa: E402
from marsrover_shared.frames import wrap_deg  # noqa: E402
from marsrover_shared.ids import IdGen, new_run_id  # noqa: E402
from marsrover_shared.stdb_http import StdbClient  # noqa: E402

# Mission (doc 00 §6.1 example) and scoring constants (doc 00 §8 / config/run.yaml).
RADIUS_M, K = 1.5, 3
W_NOV, W_VIS, W_PICK, W_CAT, W_DIST = 0.35, 0.25, 0.15, 0.15, 0.10
VISIT_RADIUS_M = 0.20

# Fake arena, rendered once in the mission frame. Same image convention as BEV tiles:
# image up = +x, image left = +y, 0.005 m/px.
RES = 0.005
ARENA_HALF_M = 1.6
ARENA_PX = int(2 * ARENA_HALF_M / RES)  # 640
BEV_PX = 360
TICK_S = 0.5  # pose + status at 2 Hz, telemetry every other tick (1 Hz)
DRIVE_MPS, TURN_DPS = 0.15, 60.0
SEEN_WITHIN_M = 0.85  # a rock shows up as a proposal when this close to a station

CATEGORIES = ["float_rock", "layered", "veined", "pebble", "bedrock"]
ROCK_RGB = {
    "float_rock": (52, 44, 40),
    "layered": (120, 92, 70),
    "veined": (95, 95, 100),
    "pebble": (80, 66, 55),
    "bedrock": (110, 100, 88),
}

# Golden assay vector (doc 00 §6.7). Standards beyond the three doc 00 prints in full are
# generated from the documented true model A = 3.0·c / (3000 + c), noise SD 0.01.
GOLDEN_STANDARDS = [
    {"c": 0, "a595": [0.0073, -0.0063, -0.001]},
    {"c": 25, "a595": [0.0368, 0.0404, 0.0364]},
    {"c": 125, "a595": [0.1281, 0.1487, 0.134]},
]
FIT = {
    "type": "linear",
    "slope": 0.0007188,
    "intercept": 0.0557,
    "r2": 0.9947,
    "fit_range": [125.0, 1000.0],
}


def curve_params(rng: random.Random) -> dict:
    standards = list(GOLDEN_STANDARDS)
    for c in (250, 500, 750, 1000, 1500, 2000):
        a = 3.0 * c / (3000 + c)
        standards.append({"c": c, "a595": [round(a + rng.gauss(0, 0.01), 4) for _ in range(3)]})
    return {
        "standard": "BSA",
        "wavelength_nm": 595,
        "linear_range": [125.0, 1000.0],
        "true_model": {"type": "hyperbolic", "amax": 3.0, "k": 3000.0, "noise_sd": 0.01},
        "fit": FIT,
        "blank_a595": -0.0068,
        "standards": standards,
    }


@dataclass
class Rock:
    x: float
    y: float
    size_m: float
    category: str
    novelty: float
    visual: float
    category_score: float
    cand_id: str = ""
    status: str = "new"
    rank: int = -1
    score: float = 0.0
    reason: str = ""
    station_id: str = ""
    thumb_url: str = ""
    components: dict = field(default_factory=dict)


def pickable(size_m: float) -> float:  # P3 role doc §4: 1.0 in [0.012, 0.030] m
    if 0.012 <= size_m <= 0.030:
        return 1.0
    if size_m < 0.012:
        return max(0.0, (size_m - 0.005) / 0.007)
    return max(0.0, (0.045 - size_m) / 0.015)


def explain(r: Rock, dist: float) -> str:  # doc 00 §8: top 2 components + category + distance
    parts = {
        f"novel texture ({r.novelty:.2f})": W_NOV * r.novelty,
        f"strong visual interest ({r.visual:.2f})": W_VIS * r.visual,
        f"pickable size {r.size_m * 100:.1f} cm": W_PICK * pickable(r.size_m),
    }
    top = sorted(parts, key=parts.get, reverse=True)[:2]
    s = f"{top[0].capitalize()}, {top[1]}; matches '{r.category}'; {dist:.1f} m away."
    return s[:140]


def make_world(rng: random.Random) -> list[Rock]:
    rocks: list[Rock] = []
    while len(rocks) < 12:
        ang, d = rng.uniform(-math.pi, math.pi), math.sqrt(rng.uniform(0.04, 1.0)) * 1.35
        x, y = d * math.cos(ang), d * math.sin(ang)
        if all(math.hypot(x - o.x, y - o.y) > 0.28 for o in rocks):
            rocks.append(
                Rock(
                    x,
                    y,
                    rng.uniform(0.012, 0.042),
                    rng.choice(CATEGORIES),
                    rng.uniform(0.2, 0.95),
                    rng.uniform(0.2, 0.95),
                    rng.uniform(0.1, 0.7),
                )
            )
    # A weak neighbour 0.12 m from the best rock, so a visit visibly excludes it.
    best = max(rocks, key=lambda r: W_NOV * r.novelty + W_VIS * r.visual)
    rocks.append(Rock(best.x + 0.12, best.y, 0.02, "pebble", 0.15, 0.2, 0.1))
    return rocks


def render_arena(rocks: list[Rock], rng: random.Random) -> Image.Image:
    noise = Image.effect_noise((ARENA_PX, ARENA_PX), 40).filter(ImageFilter.GaussianBlur(3))
    ground = Image.merge(
        "RGB", [noise.point(lambda v, o=o: o + (v - 128) // 3) for o in (176, 138, 100)]
    )
    draw = ImageDraw.Draw(ground)
    for r in rocks:  # mission (x, y) -> pixel (col, row): col = (H - y)/res, row = (H - x)/res
        col, row = (ARENA_HALF_M - r.y) / RES, (ARENA_HALF_M - r.x) / RES
        rad = r.size_m / 2 / RES * rng.uniform(1.0, 1.4)
        draw.ellipse(
            [col - rad, row - rad * 0.8, col + rad, row + rad * 0.8], fill=ROCK_RGB[r.category]
        )
    return ground


def rover_view(
    arena: Image.Image,
    px: float,
    py: float,
    heading_deg: float,
    xr: tuple[float, float],
    yr: tuple[float, float],
    size: tuple[int, int],
) -> Image.Image:
    """Render the rover-frame box xr=(xmin, xmax), yr=(ymin, ymax) from the arena.

    Output image up = rover +x, image left = rover +y (the BEV convention).
    """
    w, h = size
    hr = math.radians(heading_deg)
    ch, sh = math.cos(hr), math.sin(hr)
    sx, sy = (xr[1] - xr[0]) / h, (yr[1] - yr[0]) / w
    # Output (c, r) -> rover (xmax - r·sx, ymax - c·sy) -> mission -> arena pixel (affine).
    a, b = ch * sy / RES, sh * sx / RES
    c0 = ((ARENA_HALF_M - py) - sh * xr[1] - ch * yr[1]) / RES
    d, e = -sh * sy / RES, ch * sx / RES
    f0 = ((ARENA_HALF_M - px) - ch * xr[1] + sh * yr[1]) / RES
    return arena.transform(
        size,
        Image.Transform.AFFINE,
        (a, b, c0, d, e, f0),
        resample=Image.Resampling.BILINEAR,
        fillcolor=(30, 26, 24),
    )


def b64(img: Image.Image, fmt: str) -> str:
    buf = io.BytesIO()
    if fmt == "PNG":
        img.convert("P", palette=Image.Palette.ADAPTIVE, colors=64).save(buf, "PNG", optimize=True)
    else:
        img.save(buf, "JPEG", quality=70)
    return base64.b64encode(buf.getvalue()).decode()


def present_photo(r: Rock | None) -> str:
    img = Image.new("RGB", (240, 180), (150, 150, 155))  # scoop interior
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [20, 30, 220, 160], 18, fill=(185, 185, 190), outline=(90, 90, 95), width=3
    )
    if r is not None:
        rad = 14 + r.size_m * 600
        draw.ellipse(
            [120 - rad, 95 - rad * 0.8, 120 + rad, 95 + rad * 0.8], fill=ROCK_RGB[r.category]
        )
    return "data:image/jpeg;base64," + b64(img, "JPEG")


class FakeRun:
    def __init__(self, db: StdbClient, speed: float, seed: int) -> None:
        self.db, self.speed = db, speed
        self.rng = random.Random(seed)
        self.ids = IdGen()
        self.run_id = new_run_id(datetime.now())
        self.rocks = make_world(self.rng)
        self.arena = render_arena(self.rocks, self.rng)
        self.x = self.y = self.heading = 0.0
        self.state, self.fsm, self.active = "IDLE", "IDLE", ""
        self.tick_n = 0
        self.battery = 7.6
        self.mission_id = ""
        self.mission: tuple[str, str] = ("", "{}")  # (text, paramsJson)
        self.tasks: dict[str, tuple[str, str, int]] = {}  # task_id -> (kind, ref, seq)

    # --- plumbing --------------------------------------------------------------------
    def call(self, reducer: str, *args) -> None:
        self.db.call(reducer, *args)

    def event(self, kind: str, msg: str, level: str = "info", data: dict | None = None) -> None:
        self.call(
            "log_event", self.run_id, time.time(), "brain", level, kind, msg, json.dumps(data or {})
        )
        eventlog.log(self.run_id, "brain", level, kind, msg, data)
        print(f"  [{self.fsm:<11}] {msg}")

    def tick(self, n: int = 1) -> None:
        for _ in range(n):
            time.sleep(TICK_S / self.speed)
            now = time.time()
            self.call(
                "insert_pose", self.run_id, now, self.x, self.y, self.heading, "dr+gyro", 0.03
            )
            self.call(
                "upsert_rover_status",
                self.run_id,
                now,
                self.state,
                False,
                self.x,
                self.y,
                self.heading,
                self.fsm,
                self.active,
                True,
            )
            if self.tick_n % 2 == 0:
                self.battery -= 0.002
                imu = {
                    "ax": round(self.rng.gauss(0, 0.02), 3),
                    "ay": round(self.rng.gauss(0, 0.02), 3),
                    "az": 9.79,
                    "gz_dps": round(self.rng.gauss(0, 0.3), 2),
                }
                self.call(
                    "insert_telemetry",
                    self.run_id,
                    now,
                    self.state,
                    round(self.battery, 3),
                    55.0 + self.rng.uniform(-1, 1),
                    json.dumps(imu),
                    "DEPLOY" if self.state == "SCOOPING" else "STOW",
                    0,
                )
            self.tick_n += 1

    def task(self, task_id: str, status: str, note: str = "") -> None:
        kind, ref, seq = self.tasks[task_id]
        self.call(
            "upsert_task", task_id, self.run_id, self.mission_id, kind, ref, seq, status, note
        )

    # --- motion ------------------------------------------------------------------------
    def turn_to(self, heading: float) -> None:
        delta = wrap_deg(heading - self.heading)
        steps = max(1, round(abs(delta) / (TURN_DPS * TICK_S)))
        self.state = "MOVING"
        for _ in range(steps):
            self.heading = wrap_deg(self.heading + delta / steps)
            self.tick()

    def drive_to(self, x: float, y: float, stop_short_m: float = 0.0) -> None:
        dist = math.hypot(x - self.x, y - self.y)
        if dist < 1e-3:
            return
        self.turn_to(math.degrees(math.atan2(y - self.y, x - self.x)))
        travel = max(0.0, dist - stop_short_m)
        steps = max(1, round(travel / (DRIVE_MPS * TICK_S)))
        ux, uy = (x - self.x) / dist, (y - self.y) / dist
        self.state = "MOVING"
        for _ in range(steps):
            self.x += ux * travel / steps
            self.y += uy * travel / steps
            self.tick()

    # --- phases --------------------------------------------------------------------------
    def uplink(self) -> None:
        self.call("start_run", self.run_id, "fake_brain scripted run", time.time())
        self.tick()
        self.mission_id = self.ids.next("m")
        text = (
            "Survey within a meter and a half, find the three most interesting rocks, "
            "sample them, and come back."
        )
        params = {
            "mission_id": self.mission_id,
            "run_id": self.run_id,
            "text": text,
            "radius_m": RADIUS_M,
            "k": K,
            "categories": ["float_rock", "layered", "veined", "any"],
            "sample": True,
            "return_home": True,
            "max_duration_s": 900,
            "mode": "act",
        }
        self.mission = (text, json.dumps(params))
        self.mission_status("parsed")
        self.event("mission", f"Mission {self.mission_id} parsed: radius {RADIUS_M} m, k={K}")
        self.tick(2)
        self.mission_status("active")

    def mission_status(self, status: str) -> None:
        text, params = self.mission
        self.call("upsert_mission", self.mission_id, self.run_id, text, params, "[]", status)

    def plan_tasks(self, stations: list[tuple[float, float]]) -> list[str]:
        out = []
        for i, _ in enumerate(stations):
            tid = self.ids.next("t")
            self.tasks[tid] = ("station", f"s_{i + 1:04d}", i + 1)
            self.task(tid, "pending")
            out.append(tid)
        return out

    def survey(self) -> None:
        self.fsm = "SURVEY"
        stations = [(0.0, 0.0)] + [
            (0.6 * RADIUS_M * math.cos(a), 0.6 * RADIUS_M * math.sin(a))
            for a in (0, math.pi / 2, math.pi, -math.pi / 2)
        ]
        task_ids = self.plan_tasks(stations)
        self.event("fsm", f"IDLE -> SURVEY: {len(stations)} stations (radius {RADIUS_M} m > 0.8 m)")
        for (sx, sy), tid in zip(stations, task_ids, strict=True):
            self.task(tid, "active")
            self.drive_to(sx, sy)
            station_id = self.ids.next("s")
            self.station(station_id)
            self.task(tid, "done")

    def station(self, station_id: str) -> None:
        self.state = "STATION"
        h0, t0 = self.heading, time.time()
        for i in range(8):
            kf = self.ids.next("k")
            thumb = rover_view(
                self.arena, self.x, self.y, self.heading, (0.10, 1.30), (-0.45, 0.45), (320, 240)
            )
            self.call(
                "upsert_keyframe",
                kf,
                self.run_id,
                station_id,
                time.time(),
                self.x,
                self.y,
                self.heading,
                b64(thumb, "JPEG"),
                "",
            )
            self.tick()
            if i < 7:
                self.heading = wrap_deg(self.heading + 45)
                self.tick()
        half = BEV_PX * RES / 2
        bev = rover_view(
            self.arena, self.x, self.y, h0, (-half, half), (-half, half), (BEV_PX, BEV_PX)
        )
        self.call(
            "upsert_local_tile",
            station_id,
            self.run_id,
            t0,
            self.x,
            self.y,
            h0,
            RES,
            BEV_PX,
            BEV_PX,
            b64(bev, "PNG"),
            "",
        )
        seen = [r for r in self.rocks if math.hypot(r.x - self.x, r.y - self.y) <= SEEN_WITHIN_M]
        new = [r for r in seen if not r.cand_id]
        for r in new:
            self.score(r, station_id)
        self.event(
            "station", f"Station {station_id}: 8 keyframes, {len(seen)} proposals ({len(new)} new)"
        )
        self.rerank()

    def score(self, r: Rock, station_id: str) -> None:
        r.cand_id, r.station_id = self.ids.next("c"), station_id
        dist = math.hypot(r.x, r.y)
        p = pickable(r.size_m)
        r.score = (
            W_NOV * r.novelty
            + W_VIS * r.visual
            + W_PICK * p
            + W_CAT * r.category_score
            - W_DIST * min(dist / RADIUS_M, 1.0)
        )
        r.components = {
            "novelty": r.novelty,
            "visual": r.visual,
            "pickable": p,
            "category_score": r.category_score,
            "distance_m": round(dist, 3),
        }
        r.reason = explain(r, dist)
        crop = rover_view(self.arena, r.x, r.y, 0.0, (-0.06, 0.06), (-0.06, 0.06), (64, 64))
        r.thumb_url = "data:image/jpeg;base64," + b64(crop, "JPEG")
        self.upsert(r)

    def upsert(self, r: Rock) -> None:
        self.call(
            "upsert_candidate",
            r.cand_id,
            self.run_id,
            r.station_id,
            r.x,
            r.y,
            round(r.score, 4),
            json.dumps(r.components),
            r.category,
            r.size_m,
            r.status,
            r.rank,
            r.reason,
            r.thumb_url,
            time.time(),
        )

    def rerank(self) -> None:
        open_ = sorted(
            (r for r in self.rocks if r.cand_id and r.status in ("new", "queued")),
            key=lambda r: r.score,
            reverse=True,
        )
        for i, r in enumerate(open_):
            rank, status = (i + 1, "queued") if i < K else (-1, "new")
            if (rank, status) != (r.rank, r.status):  # only write rows whose rank/status changed
                r.rank, r.status = rank, status
                self.upsert(r)

    def investigate(self) -> None:
        self.fsm = "PLAN"
        targets = [r for r in self.rocks if r.status == "queued"]
        route, (cx, cy) = [], (self.x, self.y)
        while targets:  # nearest-neighbour route from the current pose
            nxt = min(targets, key=lambda r: math.hypot(r.x - cx, r.y - cy))
            route.append(nxt)
            targets.remove(nxt)
            cx, cy = nxt.x, nxt.y
        visit_tasks = []
        for i, r in enumerate(route):
            tid = self.ids.next("t")
            self.tasks[tid] = ("visit", r.cand_id, 6 + i)
            self.task(tid, "pending")
            visit_tasks.append(tid)
        self.event("plan", "Top-k route: " + " -> ".join(r.cand_id for r in route))
        self.tick(2)
        self.fsm = "INVESTIGATE"
        outcomes = ["sampled", "sampled", "empty_scoop"]
        for r, tid, outcome in zip(route, visit_tasks, outcomes, strict=False):
            self.visit(r, tid, outcome)

    def visit(self, r: Rock, task_id: str, outcome: str) -> None:
        self.task(task_id, "active")
        r.status, self.active = "active", r.cand_id
        self.upsert(r)
        self.drive_to(r.x, r.y, stop_short_m=0.12)
        self.state = "APPROACH"
        self.tick(4)
        self.state = "SCOOPING"
        self.tick(8)
        sample_id = self.ids.next("a")
        visit_id = self.ids.next("v")
        if outcome == "empty_scoop":
            self.event("scoop", f"{r.cand_id}: scoop came up empty, retrying once", "warn")
            self.tick(8)
            self.call(
                "add_analysis",
                self.run_id,
                sample_id,
                r.cand_id,
                visit_id,
                time.time(),
                present_photo(None),
                "",
                0.0,
                "Empty scoop.",
                False,
                "",
                -1.0,
                -1.0,
                "",
                "",
                False,
                "{}",
                True,
                0,
                "",
            )
        else:
            golden = sample_id == "a_0001"
            a595 = 0.2585 if golden else 0.5094
            conc = 282.1 if golden else round((a595 - FIT["intercept"]) / FIT["slope"], 1)
            verdict = "low" if conc < 500 else "elevated"
            seed = 3896889563 if golden else 1203475519
            self.call(
                "add_analysis",
                self.run_id,
                sample_id,
                r.cand_id,
                visit_id,
                time.time(),
                present_photo(r),
                r.category,
                0.62,
                f"A small {r.category.replace('_', ' ')} fragment.",
                True,
                "bradford_sim",
                a595,
                conc,
                "µg/mL",
                verdict,
                125 <= conc <= 1000,
                json.dumps(curve_params(random.Random(seed))),
                True,
                seed,
                "bradford_sim-1",
            )
            self.event(
                "analysis", f"{sample_id} on {r.cand_id}: SIMULATED assay {conc} µg/mL ({verdict})"
            )
        self.tick(2)
        self.call(
            "mark_visited",
            visit_id,
            self.run_id,
            r.cand_id,
            r.x,
            r.y,
            VISIT_RADIUS_M,
            outcome,
            time.time(),
        )
        r.status, r.rank, self.active = "visited", -1, ""
        for o in self.rocks:  # visited exclusion radius
            if (
                o.cand_id
                and o.status in ("new", "queued")
                and math.hypot(o.x - r.x, o.y - r.y) <= VISIT_RADIUS_M
            ):
                o.status, o.rank = "excluded", -1
                self.upsert(o)
                self.event(
                    "exclusion", f"{o.cand_id} excluded: within {VISIT_RADIUS_M} m of {visit_id}"
                )
        self.task(task_id, "done")
        self.event("visit", f"{visit_id}: {r.cand_id} {outcome}")

    def come_home(self) -> None:
        self.fsm = "RETURN"
        tid = self.ids.next("t")
        self.tasks[tid] = ("return", "home", 9)
        self.task(tid, "active")
        self.drive_to(0.0, 0.0)
        self.turn_to(0.0)
        self.task(tid, "done")
        self.state, self.fsm = "IDLE", "DONE"
        self.tick()
        self.call("end_run", self.run_id, "done", time.time())
        self.mission_status("done")
        self.event("fsm", "RETURN -> DONE: home reached, run ended")
        self.fuse()

    def fuse(self) -> None:
        """Global tiles: the arena itself, sliced into 512 px tiles. Origin = the mission (x, y)
        of each tile's top-left pixel corner; image up = +x, left = +y (axis-aligned)."""
        tile = 512
        n = 0
        for iy, row0 in enumerate(range(0, ARENA_PX, tile)):
            for ix, col0 in enumerate(range(0, ARENA_PX, tile)):
                img = self.arena.crop(
                    (col0, row0, min(col0 + tile, ARENA_PX), min(row0 + tile, ARENA_PX))
                )
                origin_x, origin_y = ARENA_HALF_M - row0 * RES, ARENA_HALF_M - col0 * RES
                self.call(
                    "upsert_global_tile",
                    f"g_{self.run_id}_{ix}_{iy}",
                    self.run_id,
                    ix,
                    iy,
                    origin_x,
                    origin_y,
                    RES,
                    img.width,
                    img.height,
                    b64(img, "PNG"),
                    1,
                )
                n += 1
        self.call("set_run_fused", self.run_id, True)
        self.event("fuse", f"Fused global map: {n} tiles (fake_brain, method dr)")

    def run(self) -> None:
        print(f"fake_brain: run {self.run_id} at {self.speed}x")
        self.uplink()
        self.survey()
        self.investigate()
        self.come_home()
        print(f"fake_brain: done. Run {self.run_id}")


def load_env() -> None:
    env = ROOT / "config" / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--speed", type=float, default=1.0, help="playback speed (default 1 = real time)"
    )
    ap.add_argument("--seed", type=int, default=7, help="arena layout seed")
    args = ap.parse_args()
    load_env()
    if not os.environ.get("STDB_TOKEN_BRAIN"):
        sys.exit("STDB_TOKEN_BRAIN is not set: fill config/.env (docs/02 §7)")
    FakeRun(StdbClient.from_env("STDB_TOKEN_BRAIN"), args.speed, args.seed).run()


if __name__ == "__main__":
    main()
