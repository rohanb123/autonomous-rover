"""Protocol tests for mocks/fake_rover.py against doc 00 sections 6.2 and 6.4-6.5.

Every reply is re-parsed with `parse_rover_message`, so a frame that drifts from the contract
fails here rather than inside P4's brain. The server runs in-process on a free port, usually
with --no-upload and a high --speed-scale, so the whole file stays well under a minute.

Plain pytest + asyncio.run: pytest-asyncio lives in files P2 doesn't own (AGENTS.md section 2).
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import email.policy
import json
import math
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import get_args

import pytest
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mocks"))

import fake_rover  # noqa: E402

from marsrover_shared.frames import wrap_deg  # noqa: E402
from marsrover_shared.schemas import (  # noqa: E402
    Ack,
    Cmd,
    Done,
    Err,
    Event,
    Op,
    StationJson,
    StationResult,
    Telemetry,
    parse_rover_message,
)

FAST = fake_rover.Options(speed_scale=20.0, no_upload=True)
REAL_TIME = fake_rover.Options(speed_scale=1.0, no_upload=True)
RUN_ID = "r_20261024_101500"

# Every op this file sends; the last test asserts it covers the whole contract.
EXERCISED: set[str] = set()


class Link:
    """A tiny brain-side client. One pump task sorts replies, telemetry and events apart."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.telemetry: list[Telemetry] = []
        self.events: list[Event] = []
        self.replies: asyncio.Queue = asyncio.Queue()
        self._n = 0
        self._pump = asyncio.create_task(self._read(), name="test-pump")

    async def _read(self) -> None:
        with contextlib.suppress(Exception):
            async for raw in self.ws:
                msg = parse_rover_message(raw)  # every frame must match the contract
                if isinstance(msg, Telemetry):
                    self.telemetry.append(msg)
                elif isinstance(msg, Event):
                    self.events.append(msg)
                else:
                    self.replies.put_nowait(msg)

    def close(self) -> None:
        self._pump.cancel()

    # --- sending ------------------------------------------------------------------------

    async def send(self, op: str, **args) -> str:
        EXERCISED.add(op)
        self._n += 1
        cmd_id = f"q_{self._n:06d}"
        await self.ws.send(Cmd(cmd_id=cmd_id, op=op, args=args).model_dump_json())
        return cmd_id

    async def send_raw(self, payload: dict) -> None:
        await self.ws.send(json.dumps(payload))

    def heartbeats(self, period: float = fake_rover.HEARTBEAT_TIMEOUT_S / 4) -> asyncio.Task:
        """Doc 00 section 4: the brain heartbeats every 0.25 s."""

        async def beat() -> None:
            while True:
                await self.send("heartbeat")
                await asyncio.sleep(period)

        return asyncio.create_task(beat(), name="test-heartbeat")

    # --- receiving ----------------------------------------------------------------------

    async def reply(self, timeout: float = 5.0):
        return await asyncio.wait_for(self.replies.get(), timeout)

    async def replies_until(self, n: int, timeout: float = 10.0) -> list:
        return [await self.reply(timeout) for _ in range(n)]

    async def quiet(self, seconds: float) -> list:
        """Everything that is not telemetry or an event, seen in `seconds` (expected: none)."""
        await asyncio.sleep(seconds)
        out = []
        while not self.replies.empty():
            out.append(self.replies.get_nowait())
        return out

    async def run(self, op: str, timeout: float = 5.0, **args):
        """send -> ack -> done/err; returns the Done or the Err."""
        cmd_id = await self.send(op, **args)
        ack = await self.reply(timeout)
        assert isinstance(ack, Ack) and ack.cmd_id == cmd_id, f"expected ack, got {ack!r}"
        final = await self.reply(timeout)
        assert final.cmd_id == cmd_id, f"{op}: reply for the wrong cmd_id: {final!r}"
        return final

    async def done(self, op: str, timeout: float = 5.0, **args) -> dict:
        final = await self.run(op, timeout=timeout, **args)
        assert isinstance(final, Done), f"{op} -> {final!r}"
        return final.result

    async def err(self, op: str, timeout: float = 5.0, **args) -> Err:
        final = await self.run(op, timeout=timeout, **args)
        assert isinstance(final, Err), f"{op} -> {final!r}"
        return final

    async def wait_telemetry(self, match, timeout: float = 3.0) -> Telemetry:
        """The next telemetry frame satisfying `match`."""

        async def poll() -> Telemetry:
            seen = 0
            while True:
                for frame in self.telemetry[seen:]:
                    if match(frame):
                        return frame
                seen = len(self.telemetry)
                await asyncio.sleep(0.02)

        return await asyncio.wait_for(poll(), timeout)


@contextlib.asynccontextmanager
async def rover(opts: fake_rover.Options = FAST, *, heartbeat: bool = True):
    """fake_rover in-process on a free port, with one client connected.

    The client heartbeats at the contract rate by default, exactly like the real brain --
    without that, any motion lasting over 1.0 s of wall clock is cut short by the watchdog,
    whatever --speed-scale says. The watchdog tests pass heartbeat=False to go quiet.
    """
    sim = fake_rover.Sim(opts)
    async with fake_rover.serve_rover(sim, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}/rover", ping_interval=None) as ws:
            link = Link(ws)
            beat = link.heartbeats() if heartbeat else None
            try:
                yield link, sim
            finally:
                if beat is not None:
                    beat.cancel()
                link.close()
                sim.close()


def run(coro, timeout: float = 25.0):
    """asyncio.run with a hard ceiling, so a hung mock fails instead of hanging the suite."""
    return asyncio.run(asyncio.wait_for(coro, timeout))


def dist(pose: dict, x: float, y: float) -> float:
    return math.dist((pose["x"], pose["y"]), (x, y))


# --- step 1: envelope, ack timing, start_run, heartbeat, bad args ---------------------------


def test_start_run_acks_then_dones():
    async def go():
        async with rover() as (link, sim):
            assert await link.done("start_run", run_id=RUN_ID) == {"ok": True}
            assert sim.st.run_id == RUN_ID

    run(go())


def test_ack_arrives_within_the_contract_budget():
    async def go():
        async with rover() as (link, _):
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            await link.send("start_run", run_id=RUN_ID)
            ack = await link.reply(fake_rover.ACK_BUDGET_S)
            assert isinstance(ack, Ack)
            assert loop.time() - t0 < fake_rover.ACK_BUDGET_S

    run(go())


def test_heartbeat_is_never_acked():
    async def go():
        async with rover(heartbeat=False) as (link, sim):
            before = sim.st.last_heartbeat
            await asyncio.sleep(0.1)  # time.monotonic is coarse on Windows (~16 ms)
            for _ in range(3):
                await link.send("heartbeat")
            assert await link.quiet(fake_rover.ACK_BUDGET_S) == []
            assert sim.st.last_heartbeat > before  # it only refreshes the watchdog

    run(go())


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({"op": "drive", "args": {}}, id="missing-arg"),
        pytest.param({"op": "drive", "args": {"dist_m": 0.3, "speed": 4.0}}, id="speed-range"),
        pytest.param(
            {"op": "turn", "args": {"deg": 90, "speed": 0.5, "nope": 1}}, id="unknown-arg"
        ),
        pytest.param({"op": "fly", "args": {}}, id="unknown-op"),
        pytest.param({"op": "scoop_pose", "args": {"pose": "WIGGLE"}}, id="bad-enum"),
        pytest.param({"op": "drive", "args": {"dist_m": "far", "speed": 0.5}}, id="wrong-type"),
    ],
)
def test_bad_args_get_e_bad_args(args):
    payload = {"type": "cmd", "src": "brain", "cmd_id": "q_000001", **args}

    async def go():
        async with rover() as (link, _):
            await link.send_raw(payload)
            msg = await link.reply(fake_rover.ACK_BUDGET_S)
            assert isinstance(msg, Err), msg
            assert msg.code == "E_BAD_ARGS"
            assert msg.cmd_id == "q_000001"

    run(go())


def test_unparseable_frame_without_a_cmd_id_is_dropped():
    async def go():
        async with rover() as (link, _):
            await link.ws.send("{not json")
            await link.send_raw({"type": "cmd", "src": "brain", "op": "drive", "args": {}})
            assert await link.quiet(fake_rover.ACK_BUDGET_S) == []

    run(go())


def test_set_pose_overwrites_the_pose():
    async def go():
        async with rover() as (link, sim):
            result = await link.done("set_pose", x=0.5, y=-0.25, heading_deg=31.5, source="aruco")
            assert result["pose"]["x"] == pytest.approx(0.5)
            assert result["pose"]["y"] == pytest.approx(-0.25)
            assert result["pose"]["heading_deg"] == pytest.approx(31.5)
            assert result["pose"]["source"] == "aruco"
            assert sim.st.pose.x == pytest.approx(0.5)

    run(go())


# --- step 2: telemetry ----------------------------------------------------------------------


def test_telemetry_runs_at_5hz_and_validates():
    async def go():
        async with rover() as (link, _):
            await link.done("start_run", run_id=RUN_ID)
            link.telemetry.clear()
            await asyncio.sleep(1.0)
            frames = list(link.telemetry)
            assert len(frames) >= 4, f"expected >= 4 frames in 1 s, got {len(frames)}"
            for f in frames:
                assert f.run_id == RUN_ID and f.src == "rover"
                assert f.state == "IDLE" and not f.estop
                assert f.imu.az == pytest.approx(9.79, abs=0.5)
                assert f.scoop.pose == "STOW" and len(f.scoop.us) == 2
                assert f.queue_len == 0 and f.uplink_backlog == 0

    run(go())


# --- step 3: drive, turn, stop, E_BUSY ------------------------------------------------------


def test_drive_moves_along_x():
    async def go():
        async with rover() as (link, _):
            result = await link.done("drive", dist_m=0.3, speed=1.0)
            assert result["pose"]["x"] == pytest.approx(0.3, abs=1e-6)
            assert result["pose"]["y"] == pytest.approx(0.0, abs=1e-6)

    run(go())


def test_negative_dist_drives_backward():
    async def go():
        async with rover() as (link, _):
            result = await link.done("drive", dist_m=-0.2, speed=1.0)
            assert result["pose"]["x"] == pytest.approx(-0.2, abs=1e-6)

    run(go())


def test_turn_90_then_drive_moves_along_y():
    async def go():
        async with rover() as (link, _):
            turned = await link.done("turn", deg=90.0, speed=1.0)
            assert turned["pose"]["heading_deg"] == pytest.approx(90.0, abs=1e-6)
            assert turned["method"] in ("gyro", "timed")
            moved = await link.done("drive", dist_m=0.25, speed=1.0)
            assert moved["pose"]["x"] == pytest.approx(0.0, abs=1e-6)
            assert moved["pose"]["y"] == pytest.approx(0.25, abs=1e-6)

    run(go())


def test_heading_wraps_into_the_contract_range():
    async def go():
        async with rover() as (link, _):
            result = await link.done("turn", deg=270.0, speed=1.0)
            heading = result["pose"]["heading_deg"]
            assert heading == pytest.approx(-90.0, abs=1e-6)
            assert -180.0 < heading <= 180.0

    run(go())


def test_gyro_reads_nonzero_while_turning():
    async def go():
        async with rover(fake_rover.Options(speed_scale=2.0, no_upload=True)) as (link, _):
            await link.send("turn", deg=180.0, speed=1.0)
            frame = await link.wait_telemetry(lambda f: f.state == "MOVING")
            assert frame.imu.gz_dps != 0.0
            await link.replies_until(2)  # ack + done
            link.telemetry.clear()  # only look at frames from after the turn
            idle = await link.wait_telemetry(lambda f: f.state == "IDLE")
            assert idle.imu.gz_dps == 0.0

    run(go())


def test_a_second_long_op_while_busy_gets_e_busy():
    async def go():
        async with rover() as (link, _):
            first = await link.send("drive", dist_m=1.5, speed=0.3)
            second = await link.send("station", station_id="s_0001")
            msgs = await link.replies_until(4)
            errs = [m for m in msgs if isinstance(m, Err)]
            dones = [m for m in msgs if isinstance(m, Done)]
            assert [e.cmd_id for e in errs] == [second]
            assert errs[0].code == "E_BUSY"
            assert [d.cmd_id for d in dones] == [first]

    run(go())


def test_stop_mid_drive_ends_the_drive_with_a_done():
    async def go():
        async with rover() as (link, _):
            driving = await link.send("drive", dist_m=1.5, speed=0.3)
            assert isinstance(await link.reply(), Ack)
            await asyncio.sleep(0.15)
            stopping = await link.send("stop")
            msgs = await link.replies_until(3)
            by_id = {m.cmd_id: m for m in msgs if not isinstance(m, Ack)}
            assert isinstance(by_id[stopping], Done)
            assert isinstance(by_id[driving], Done), "a stopped drive still reports done"
            stopped_at = by_id[stopping].result["pose"]
            ended_at = by_id[driving].result["pose"]
            assert ended_at["x"] < 1.4, "the drive should have been cut short"
            assert ended_at["x"] == pytest.approx(stopped_at["x"], abs=0.02)

    run(go())


# --- step 4: E-STOP latch and the heartbeat watchdog ----------------------------------------


def test_estop_mid_drive_latches_and_aborts_the_drive():
    async def go():
        async with rover() as (link, sim):
            driving = await link.send("drive", dist_m=1.5, speed=0.3)
            assert isinstance(await link.reply(), Ack)
            await asyncio.sleep(0.15)
            stopping = await link.send("estop")
            msgs = await link.replies_until(3)
            by_id = {m.cmd_id: m for m in msgs if not isinstance(m, Ack)}
            assert by_id[stopping].result == {"latched": True}
            assert isinstance(by_id[driving], Err)
            assert by_id[driving].code == "E_ESTOP"
            assert sim.st.estop and sim.st.state == "ESTOP"
            frame = await link.wait_telemetry(lambda f: f.estop)
            assert frame.state == "ESTOP"

    run(go())


def test_ops_are_refused_while_latched_then_clear_estop_restores():
    async def go():
        async with rover() as (link, _):
            assert await link.done("estop") == {"latched": True}
            for op, args in (
                ("drive", {"dist_m": 0.1, "speed": 0.5}),
                ("turn", {"deg": 10.0, "speed": 0.5}),
                ("station", {"station_id": "s_0001"}),
                ("dump", {}),
                ("stop", {}),
            ):
                assert (await link.err(op, **args)).code == "E_ESTOP", op
            # Doc-exempt ops still work while latched.
            assert "url" in await link.done("snapshot")
            assert await link.done("set_pose", x=0.0, y=0.0, heading_deg=0.0, source="aruco")
            assert await link.done("clear_estop") == {"latched": False}
            restored = await link.done("drive", dist_m=0.1, speed=1.0)
            assert restored["pose"]["x"] == pytest.approx(0.1, abs=1e-6)

    run(go())


def test_drive_without_heartbeats_stops_after_one_second():
    async def go():
        async with rover(REAL_TIME, heartbeat=False) as (link, sim):
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            final = await link.err("drive", timeout=5.0, dist_m=1.5, speed=1.0)
            elapsed = loop.time() - t0
            assert final.code == "E_TIMEOUT", final
            assert 0.8 < elapsed < 2.5, f"watchdog fired after {elapsed:.2f} s"
            assert any(e.kind == "stall" for e in link.events), link.events
            assert sim.st.state == "IDLE"

    run(go())


def test_drive_with_heartbeats_completes():
    async def go():
        async with rover(REAL_TIME) as (link, _):
            result = await link.done("drive", timeout=6.0, dist_m=0.45, speed=1.0)
            assert result["pose"]["x"] == pytest.approx(0.45, abs=1e-6)
            assert not [e for e in link.events if e.kind == "stall"]

    run(go())


def test_idle_without_heartbeats_does_not_trip_the_watchdog():
    async def go():
        async with rover(REAL_TIME, heartbeat=False) as (link, sim):
            await asyncio.sleep(fake_rover.HEARTBEAT_TIMEOUT_S + 0.5)
            assert sim.st.state == "IDLE" and not sim.st.estop
            assert [e for e in link.events if e.kind == "stall"] == []
            assert await link.quiet(0.1) == []

    run(go())


# --- step 5: station, uploads, retry queue --------------------------------------------------


async def _station(link, station_id: str = "s_0001", **args) -> dict:
    return await link.done("station", timeout=20.0, station_id=station_id, **args)


def test_station_result_matches_the_contract():
    async def go():
        async with rover() as (link, _):
            await link.done("start_run", run_id=RUN_ID)
            raw = await _station(link, "s_0002")
            parsed = StationResult.model_validate(raw)  # doc 00 section 6.4
            assert parsed.run_id == RUN_ID and parsed.station_id == "s_0002"
            assert len(parsed.keyframes) == 8  # StationArgs defaults: n_views 8, step 45
            assert parsed.proposals, "a station should propose at least one rock"
            for p in parsed.proposals:
                assert p.kf_id in {kf.kf_id for kf in parsed.keyframes}
                assert p.crop == f"crop_{p.prop_id}.jpg"
                assert len(p.bbox_px) == 4
                assert 0.0 <= p.contrast <= 1.0 and p.size_m > 0

    run(go())


def test_station_keyframe_headings_are_absolute_and_step_apart():
    async def go():
        async with rover() as (link, _):
            await link.done("start_run", run_id=RUN_ID)
            await link.done("set_pose", x=0.0, y=0.0, heading_deg=30.0, source="aruco")
            raw = await _station(link, "s_0001", n_views=8, step_deg=45.0)
            headings = [kf["heading_deg"] for kf in raw["keyframes"]]
            # Contract 1.3.0: absolute mission-frame heading at capture, wrapped.
            assert headings == pytest.approx([wrap_deg(30.0 + 45.0 * i) for i in range(8)])
            assert all(-180.0 < h <= 180.0 for h in headings)

    run(go())


def test_station_honours_n_views_and_step_deg():
    async def go():
        async with rover() as (link, _):
            await link.done("start_run", run_id=RUN_ID)
            raw = await _station(link, "s_0001", n_views=4, step_deg=90.0)
            headings = [kf["heading_deg"] for kf in raw["keyframes"]]
            assert headings == pytest.approx([0.0, 90.0, 180.0, -90.0])

    run(go())


def test_station_bev_is_a_360px_tile_at_5mm_per_pixel():
    async def go():
        async with rover() as (link, _):
            await link.done("start_run", run_id=RUN_ID)
            raw = await _station(link)
            assert raw["bev"] == {
                "file": "bev.png",
                "res_m_px": 0.005,
                "w_px": 360,
                "h_px": 360,
                "frame": "rover@station",
            }

    run(go())


def test_pi_minted_ids_keep_incrementing_across_stations():
    async def go():
        async with rover() as (link, _):
            await link.done("start_run", run_id=RUN_ID)
            first = await _station(link, "s_0001")
            second = await _station(link, "s_0002")
            kfs = [kf["kf_id"] for kf in first["keyframes"] + second["keyframes"]]
            assert kfs == [f"k_{i:04d}" for i in range(1, 17)]
            props = [p["prop_id"] for p in first["proposals"] + second["proposals"]]
            assert props == [f"p_{i:04d}" for i in range(1, len(props) + 1)]

    run(go())


def test_station_proposals_are_deduped_and_in_the_mission_frame():
    async def go():
        async with rover() as (link, _):
            await link.done("start_run", run_id=RUN_ID)
            await link.done("set_pose", x=0.4, y=-0.2, heading_deg=0.0, source="aruco")
            raw = await _station(link)
            xys = [tuple(p["xy"]) for p in raw["proposals"]]
            for i, a in enumerate(xys):
                for b in xys[i + 1 :]:
                    assert math.dist(a, b) > fake_rover.STATION_DEDUPE_M
                # Rocks sit 0.3-1.1 m ahead of the station, so never on top of it.
                assert 0.2 < math.dist(a, (0.4, -0.2)) < 1.6

    run(go())


def test_station_falls_back_to_mapper_not_ok_and_queues_a_retry():
    unreachable = fake_rover.Options(speed_scale=20.0, mapper="http://127.0.0.1:1")

    async def go():
        async with rover(unreachable) as (link, sim):
            await link.done("start_run", run_id=RUN_ID)
            raw = await _station(link)
            parsed = StationResult.model_validate(raw)
            assert parsed.mapper.ok is False
            assert parsed.mapper.stored == "" and parsed.mapper.urls.bev == ""
            assert len(sim.st.retry_queue) == 1
            retries = [e for e in link.events if e.kind == "upload_retry"]
            assert retries, link.events
            assert retries[0].detail["endpoint"] == "/ingest/station"
            frame = await link.wait_telemetry(lambda f: f.uplink_backlog > 0)
            assert frame.uplink_backlog == 1

    run(go())


class _Recorder(BaseHTTPRequestHandler):
    """A stand-in mapper that records the multipart parts and answers doc 00 section 6.4."""

    parts: dict[str, bytes] = {}

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        body = self.rfile.read(int(self.headers["Content-Length"]))
        header = f"Content-Type: {self.headers['Content-Type']}\r\nMIME-Version: 1.0\r\n\r\n"
        msg = email.message_from_bytes(header.encode() + body, policy=email.policy.default)
        _Recorder.parts = {
            part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
            for part in msg.iter_parts()
        }
        station = json.loads(_Recorder.parts["station.json"])
        rel = f"/data/runs/{station['run_id']}/stations/{station['station_id']}"
        reply = json.dumps(
            {
                "ok": True,
                "station_id": station["station_id"],
                "stored": f"{rel}/",
                "urls": {
                    "bev": f"http://127.0.0.1/files{rel}/bev.png",
                    "crops": {
                        p["prop_id"]: f"http://127.0.0.1/files{rel}/{p['crop']}"
                        for p in station["proposals"]
                    },
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args) -> None:
        pass


@contextlib.contextmanager
def recording_mapper():
    _Recorder.parts = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_station_upload_posts_the_contract_multipart():
    async def go(mapper: str):
        async with rover(fake_rover.Options(speed_scale=20.0, mapper=mapper)) as (link, _):
            await link.done("start_run", run_id=RUN_ID)
            return await _station(link, "s_0003", n_views=4, step_deg=90.0)

    with recording_mapper() as mapper:
        raw = run(go(mapper))
    parsed = StationResult.model_validate(raw)
    parts = _Recorder.parts
    assert parsed.mapper.ok is True
    assert parsed.mapper.urls.bev.endswith("/bev.png")
    assert set(parsed.mapper.urls.crops) == {p.prop_id for p in parsed.proposals}
    # Doc 00 section 6.4: station.json, kf_000..kf_00N.jpg, bev.png, one crop per proposal.
    expected = {"station.json", "bev.png"}
    expected |= {f"kf_{i:03d}.jpg" for i in range(4)}
    expected |= {f"crop_{p.prop_id}.jpg" for p in parsed.proposals}
    assert set(parts) == expected
    sent = StationJson.model_validate_json(parts["station.json"])
    assert sent.model_dump() == parsed.model_dump(exclude={"mapper"})
    assert all(parts[kf.file] for kf in sent.keyframes), "keyframe parts must not be empty"
    assert parts["bev.png"].startswith(b"\x89PNG")


# --- step 6: approach, scoop, dump, scoop_pose, snapshot, per-op timeouts -------------------


def test_approach_ends_in_the_scoop_zone():
    async def go():
        async with rover() as (link, sim):
            await link.done("start_run", run_id=RUN_ID)
            result = await link.done("approach", timeout=15.0, cand_id="c_0007", x=0.5, y=0.3)
            assert result["in_zone"] is True
            assert len(result["offset_px"]) == 2
            assert all(abs(v) <= 2 for v in result["offset_px"])
            gap = dist(result["pose"], 0.5, 0.3)
            assert gap == pytest.approx(fake_rover.SCOOP_STANDOFF_M, abs=0.03)
            assert sim.st.last_cand_id == "c_0007"  # `scoop` needs it for sample.json

    run(go())


def test_scoop_cycles_the_poses_and_reports_the_present_image():
    async def go():
        async with rover(fake_rover.Options(speed_scale=4.0, no_upload=True)) as (link, sim):
            await link.done("start_run", run_id=RUN_ID)
            await link.done("approach", timeout=15.0, cand_id="c_0007", x=0.4, y=0.0)
            link.telemetry.clear()
            result = await link.done("scoop", timeout=20.0, sample_id="a_0001")
            assert result["present_image"].endswith(".jpg")
            assert result["uploaded"] is True  # --no-upload fakes a stored sample
            assert "a_0001" in result["present_image"]
            assert dist(result["pose"], 0.0, 0.0) > 0.0
            poses = {f.scoop.pose for f in link.telemetry}
            assert poses & {"DEPLOY", "CURL", "PRESENT"}, poses
            assert any(f.state == "SCOOPING" for f in link.telemetry)
            assert sim.st.scoop.pose == "PRESENT"

    run(go())


def test_dump_and_scoop_pose_return_ok_and_move_the_scoop():
    async def go():
        async with rover() as (link, sim):
            assert await link.done("scoop_pose", pose="DEPLOY") == {"ok": True}
            assert sim.st.scoop.pose == "DEPLOY"
            assert sim.st.scoop.us == fake_rover.SCOOP_US["DEPLOY"]
            assert await link.done("dump", timeout=10.0) == {"ok": True}
            assert sim.st.scoop.pose == "STOW"  # dump ends stowed

    run(go())


def test_snapshot_returns_the_pi_debug_url():
    async def go():
        async with rover() as (link, _):
            result = await link.done("snapshot")
            assert result["url"].startswith(fake_rover.SNAPSHOT_URL)

    run(go())


def test_an_op_that_overruns_its_deadline_gets_e_timeout(monkeypatch):
    monkeypatch.setitem(fake_rover.OP_TIMEOUT_S, "scoop", 0.05)

    async def go():
        async with rover() as (link, sim):
            await link.done("start_run", run_id=RUN_ID)
            final = await link.err("scoop", timeout=10.0, sample_id="a_0001")
            assert final.code == "E_TIMEOUT"
            assert sim.st.state == "IDLE"
            # The slot is released, so the next op is not E_BUSY.
            assert await link.done("snapshot")

    run(go())


# --- step 7: the CLI knobs ------------------------------------------------------------------


def test_cli_flags_map_onto_options():
    args = fake_rover.parse_args(
        [
            "--host",
            "127.0.0.1",
            "--port",
            "9999",
            "--mapper",
            "http://192.168.50.10:8200",
            "--speed-scale",
            "20",
            "--noise",
            "0.01",
            "--fail-rate",
            "0.25",
            "--seed",
            "7",
            "--no-upload",
        ]
    )
    assert (args.host, args.port) == ("127.0.0.1", 9999)
    assert fake_rover.options_from(args) == fake_rover.Options(
        mapper="http://192.168.50.10:8200",
        speed_scale=20.0,
        noise=0.01,
        fail_rate=0.25,
        seed=7,
        no_upload=True,
    )
    assert fake_rover.parse_args([]).port == 8765  # doc 00 section 3
    assert fake_rover.options_from(fake_rover.parse_args([])).speed_scale == 1.0


def _stable(station: dict) -> dict:
    """A station result minus the wall-clock stamps, so two seeded runs can be compared."""
    out = {k: v for k, v in station.items() if k != "t"}
    out["keyframes"] = [{k: v for k, v in kf.items() if k != "t"} for kf in station["keyframes"]]
    return out


def test_the_same_seed_replays_the_same_station():
    async def go(seed: int) -> dict:
        opts = fake_rover.Options(speed_scale=20.0, no_upload=True, seed=seed)
        async with rover(opts) as (link, _):
            await link.done("start_run", run_id=RUN_ID)
            return await _station(link, "s_0001", n_views=4, step_deg=90.0)

    first, again, other = run(go(11)), run(go(11)), run(go(12))
    assert _stable(first) == _stable(again)
    assert _stable(first) != _stable(other), "a different seed should change the station"


def test_fail_rate_one_makes_approach_and_scoop_report_their_codes():
    always = fake_rover.Options(speed_scale=20.0, no_upload=True, fail_rate=1.0, seed=3)

    async def go():
        async with rover(always) as (link, _):
            await link.done("start_run", run_id=RUN_ID)
            lost = await link.err("approach", timeout=15.0, cand_id="c_0001", x=0.4, y=0.0)
            assert lost.code == "E_LOST_TARGET"
            scooped = await link.err("scoop", timeout=15.0, sample_id="a_0001")
            assert scooped.code in ("E_SCOOP", "E_UPLOAD")

    run(go())


def test_fail_rate_zero_never_fails_approach():
    async def go():
        async with rover() as (link, _):
            await link.done("start_run", run_id=RUN_ID)
            for i in range(3):
                result = await link.done(
                    "approach", timeout=15.0, cand_id=f"c_{i:04d}", x=0.3, y=0.1 * i
                )
                assert result["in_zone"] is True

    run(go())


def test_speed_scale_shortens_a_drive():
    """0.9 m at speed 1.0 is 3 s of simulated time; --speed-scale divides the wall clock."""
    sim_s = 0.9 / fake_rover.DRIVE_MPS

    async def timed(scale: float) -> float:
        async with rover(fake_rover.Options(speed_scale=scale, no_upload=True)) as (link, _):
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            await link.done("drive", timeout=15.0, dist_m=0.9, speed=1.0)
            return loop.time() - t0

    slow, fast = run(timed(4.0)), run(timed(16.0))
    # Absolute: both finish well inside the un-scaled duration. Relative: 16x beats 4x.
    # The ratio is bounded loosely because the OS timer floor, not the scale, sets the
    # per-slice cost on a loaded machine.
    assert fast < sim_s / 2, f"scale 16 took {fast:.2f} s of a {sim_s:.1f} s simulated drive"
    assert slow < sim_s, f"scale 4 took {slow:.2f} s of a {sim_s:.1f} s simulated drive"
    assert 1.5 < slow / fast < 10.0, f"ratio {slow / fast:.2f} should track the 4x change"


def test_noise_perturbs_the_pose_and_the_imu():
    noisy = fake_rover.Options(speed_scale=20.0, no_upload=True, noise=0.05, seed=5)

    async def go():
        async with rover(noisy) as (link, _):
            result = await link.done("drive", dist_m=0.6, speed=1.0)
            assert result["pose"]["y"] != 0.0  # lateral slip
            await asyncio.sleep(0.5)
            assert any(f.imu.ax != 0.0 for f in link.telemetry)

    run(go())


# --- step 8: contract coverage (keep last: it reads what the tests above sent) ---------------


def test_every_op_in_the_contract_is_exercised():
    missing = set(get_args(Op)) - EXERCISED
    assert not missing, f"ops never sent by this file: {sorted(missing)}"
