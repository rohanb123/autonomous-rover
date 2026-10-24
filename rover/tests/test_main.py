"""Tests for rover/rover/main.py -- device selection, honest refusals, and the I1 acceptance.

The last test is the one the milestone actually asks for (P2 role doc section 7, "by I1"):

    "`drive 0.3` and `turn 90` work from a WS client; watchdog stops the motors when you
     kill the client mid-drive"

It runs the *real* ws_server over a real socket against the *real* HardwareBackend, Safety,
Motion and Motors, with only the pin writes recorded instead of energising a TB6612. So the
only part of the I1 acceptance left for P1's harness is whether the wheels physically turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402

from marsrover_shared.schemas import (  # noqa: E402
    Ack,
    Cmd,
    Done,
    Err,
    Telemetry,
    parse_rover_message,
)
from rover import main as rover_main  # noqa: E402
from rover import ws_server  # noqa: E402
from rover.config import parse_calib  # noqa: E402

CALIB = parse_calib(helpers.CALIB_DICT)
PINS = CALIB.pins
RUN_ID = "r_20261024_101500"
ALL_OFF = dict.fromkeys(("motors", "servos", "imu", "camera"), "off")


def backend(**modes: str) -> rover_main.HardwareBackend:
    return rover_main.build_backend(CALIB, {**ALL_OFF, **modes})


# --- device selection ----------------------------------------------------------------------


def test_everything_off_builds_on_a_laptop():
    b = backend()
    try:
        assert b.inventory() == {
            "motors": False,
            "servos": False,
            "imu": False,
            "camera": False,
            "exposure_locked": False,
            "calibrated": False,
        }
    finally:
        b.close()


def test_auto_degrades_instead_of_crashing(caplog: pytest.LogCaptureFixture):
    """No GPIO, no I2C, no camera on this laptop: `auto` must log and carry on."""
    b = rover_main.build_backend(CALIB, dict.fromkeys(ALL_OFF, "auto"))
    try:
        inv = b.inventory()
        assert inv["motors"] is False and inv["servos"] is False
    finally:
        b.close()


def test_sim_motors_are_present_so_the_timing_loop_actually_runs():
    b = backend(motors="sim")
    try:
        assert b.motors.present is True
    finally:
        b.close()


def test_asking_for_an_unbuilt_imu_backend_fails_loudly():
    with pytest.raises(SystemExit, match="imu.py"):
        rover_main.make_imu("mpu6050", CALIB)


def test_an_uncalibrated_calib_warns(caplog: pytest.LogCaptureFixture):
    with caplog.at_level("WARNING"):
        b = backend()
    b.close()
    assert any("placeholder" in r.message for r in caplog.records)


# --- the CLI -------------------------------------------------------------------------------


def test_defaults_match_doc00():
    args = rover_main.parse_args([])
    assert args.port == 8765  # docs/00 section 3
    assert args.host == "0.0.0.0"
    assert rover_main.modes_from(args) == dict.fromkeys(ALL_OFF, "auto")


def test_no_hw_turns_everything_off():
    args = rover_main.parse_args(["--no-hw", "--motors", "gpio"])
    assert rover_main.modes_from(args) == ALL_OFF


def test_flags_and_env_vars_map_onto_modes(monkeypatch: pytest.MonkeyPatch):
    args = rover_main.parse_args(["--motors", "sim", "--camera", "off", "--port", "9999"])
    assert args.port == 9999
    assert rover_main.modes_from(args)["motors"] == "sim"
    assert rover_main.modes_from(args)["camera"] == "off"

    monkeypatch.setenv("ROVER_MOTORS", "off")
    monkeypatch.setenv("ROVER_CAMERA", "sim")
    modes = rover_main.modes_from(rover_main.parse_args([]))
    assert modes["motors"] == "off" and modes["camera"] == "sim"


def test_an_unknown_device_mode_is_rejected():
    with pytest.raises(SystemExit):
        rover_main.parse_args(["--motors", "teleport"])


# --- honest refusals -----------------------------------------------------------------------


@pytest.mark.parametrize("op", sorted(rover_main.UNBUILT))
def test_unbuilt_ops_name_what_is_missing(op: str, tmp_path: Path, monkeypatch):
    """Not E_LOST_TARGET or E_SCOOP: fsm.py maps those onto physical outcomes and carries on,
    so using them would make the brain invent sample results for code that does not exist."""
    monkeypatch.setenv("MARSROVER_DATA_DIR", str(tmp_path))
    b = backend()
    try:
        b.start_run(RUN_ID)
        args = {
            "station": {"station_id": "s_0001", "n_views": 8, "step_deg": 45.0},
            "approach": {"cand_id": "c_0001", "x": 0.4, "y": 0.0},
            "scoop": {"sample_id": "a_0001"},
            "scoop_pose": {"pose": "STOW"},
        }.get(op, {})
        handler = getattr(b, op)
        with pytest.raises(ws_server.OpError) as exc:
            result = handler(**args)
            if asyncio.iscoroutine(result):
                asyncio.run(result)
        assert exc.value.code == "E_INTERNAL"
        assert exc.value.msg, "the message is where the honesty lives"
    finally:
        b.close()

    lines = (tmp_path / "runs" / RUN_ID / "events.jsonl").read_text(encoding="utf-8").splitlines()
    kinds = [json.loads(line)["kind"] for line in lines]
    assert "unimplemented" in kinds


def test_approach_still_remembers_the_cand_id_it_was_given():
    """scoop's sample.json needs a cand_id that its own args don't carry (doc 00 section 6.5)."""
    b = backend()
    try:
        with pytest.raises(ws_server.OpError):
            asyncio.run(b.approach("c_0042", 0.5, 0.1))
        assert b.last_cand_id == "c_0042"
    finally:
        b.close()


def test_drive_without_motors_says_which_device_is_missing():
    b = backend(motors="off")
    try:
        with pytest.raises(ws_server.OpError) as exc:
            asyncio.run(b.drive(0.3, 0.5))
        assert exc.value.code == "E_INTERNAL"
        assert "motors" in exc.value.msg and "--motors sim" in exc.value.msg
    finally:
        b.close()


# --- telemetry -----------------------------------------------------------------------------


def test_telemetry_validates_and_reports_no_imu_as_zeros():
    """All zeros, not a fabricated 9.79: a reader can tell there is no accelerometer."""
    b = backend()
    try:
        t = b.telemetry()
        assert isinstance(t, Telemetry)
        assert (t.imu.ax, t.imu.ay, t.imu.az, t.imu.gz_dps) == (0.0, 0.0, 0.0, 0.0)
        assert t.state == "IDLE" and t.estop is False
        assert t.scoop.us == [1500, 1500]  # calib's STOW until servos.py is driving
        assert t.uplink_backlog == 0  # no upload queue until uploader.py lands at I2
    finally:
        b.close()


def test_start_run_rezeroes_the_pose():
    b = backend()
    try:
        b.set_pose(1.0, 2.0, 90.0, "aruco")
        assert b.telemetry().pose.x == 1.0
        assert b.start_run(RUN_ID) == {"ok": True}
        p = b.telemetry().pose
        assert (p.x, p.y, p.heading_deg) == (0.0, 0.0, 0.0)
        assert b.telemetry().run_id == RUN_ID
    finally:
        b.close()


def test_estop_latches_before_it_cuts_power():
    b = backend(motors="sim")
    try:
        assert b.estop() == {"latched": True}
        assert b.estopped and b.state == "ESTOP"
        assert b.motors.latches == {"estop"}
        pins = b.motors._backend
        pins.clear()
        b.motors.set(1.0, 1.0)  # what the worker thread would try 20 ms later
        assert pins.writes == []
        assert b.clear_estop() == {"latched": False}
        assert not b.estopped and b.state == "IDLE"
    finally:
        b.close()


# --- the I1 acceptance, end to end ---------------------------------------------------------


class Link:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.replies: asyncio.Queue = asyncio.Queue()
        self.events: list = []
        self._n = 0
        self._pump = asyncio.create_task(self._read())

    async def _read(self) -> None:
        with contextlib.suppress(Exception):
            async for raw in self.ws:
                msg = parse_rover_message(raw)
                if isinstance(msg, Telemetry):
                    continue
                if isinstance(msg, Ack | Done | Err):
                    self.replies.put_nowait(msg)
                else:
                    self.events.append(msg)

    def close(self) -> None:
        self._pump.cancel()

    async def send(self, op: str, **args) -> str:
        self._n += 1
        cmd_id = f"q_{self._n:06d}"
        await self.ws.send(Cmd(cmd_id=cmd_id, op=op, args=args).model_dump_json())
        return cmd_id

    def heartbeats(self) -> asyncio.Task:
        async def beat() -> None:
            while True:
                await self.send("heartbeat")
                await asyncio.sleep(ws_server.HEARTBEAT_TIMEOUT_S / 4)

        return asyncio.create_task(beat())

    async def reply(self, timeout: float = 10.0):
        return await asyncio.wait_for(self.replies.get(), timeout)

    async def run(self, op: str, timeout: float = 10.0, **args):
        cmd_id = await self.send(op, **args)
        ack = await self.reply(timeout)
        assert isinstance(ack, Ack) and ack.cmd_id == cmd_id, f"expected ack, got {ack!r}"
        final = await self.reply(timeout)
        assert final.cmd_id == cmd_id
        return final


def test_the_i1_acceptance_drive_and_turn_over_a_real_socket():
    """`drive 0.3` and `turn 90` from a WS client, through the real server and Motion."""

    async def go():
        b = backend(motors="sim")
        try:
            async with ws_server.serve_rover(b, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                async with connect(f"ws://127.0.0.1:{port}/rover", ping_interval=None) as ws:
                    link = Link(ws)
                    beat = link.heartbeats()
                    try:
                        assert (await link.run("start_run", run_id=RUN_ID)).result == {"ok": True}

                        driven = await link.run("drive", dist_m=0.3, speed=0.5)
                        assert isinstance(driven, Done), driven
                        assert driven.result["pose"]["x"] == pytest.approx(0.3, abs=0.02)

                        turned = await link.run("turn", deg=90.0, speed=0.5)
                        assert isinstance(turned, Done), turned
                        assert turned.result["pose"]["heading_deg"] == pytest.approx(90.0, abs=1.0)
                        # No IMU, so docs/05 U-2's timed fallback is what ran.
                        assert turned.result["method"] == "timed"

                        # Facing +90, so the next drive goes to +y (left).
                        moved = await link.run("drive", dist_m=0.2, speed=0.5)
                        assert moved.result["pose"]["y"] == pytest.approx(0.2, abs=0.02)
                        assert moved.result["pose"]["x"] == pytest.approx(0.3, abs=0.02)
                    finally:
                        beat.cancel()
                        link.close()
        finally:
            b.close()

    asyncio.run(asyncio.wait_for(go(), 40.0))


def test_the_i1_acceptance_killing_the_client_mid_drive_stops_the_motors():
    """The other half: "watchdog stops the motors when you kill the client mid-drive"."""

    async def go():
        b = backend(motors="sim")
        pins = b.motors._backend
        try:
            async with ws_server.serve_rover(b, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                async with connect(f"ws://127.0.0.1:{port}/rover", ping_interval=None) as ws:
                    link = Link(ws)
                    await link.send("start_run", run_id=RUN_ID)
                    await link.reply()
                    await link.reply()
                    # A long drive, and deliberately no heartbeats from here on.
                    await link.send("drive", dist_m=3.0, speed=0.5)
                    assert isinstance(await link.reply(), Ack)
                    await asyncio.sleep(0.2)
                    assert b.motors.last != (0.0, 0.0), "it should be driving"
                    final = await link.reply(timeout=5.0)
                assert isinstance(final, Err), final
                assert final.code == "E_TIMEOUT"
            # The hardware is left safe: braked, standby low, and latched off.
            assert pins.last("pwm", PINS.pwm_a) == 0.0
            assert pins.last("out", PINS.stby) is False
            assert "watchdog" in b.motors.latches
            assert b.state == "IDLE"
        finally:
            b.close()

    asyncio.run(asyncio.wait_for(go(), 40.0))


def test_a_stop_frame_ends_the_drive_with_a_done():
    async def go():
        b = backend(motors="sim")
        try:
            async with ws_server.serve_rover(b, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                async with connect(f"ws://127.0.0.1:{port}/rover", ping_interval=None) as ws:
                    link = Link(ws)
                    beat = link.heartbeats()
                    try:
                        driving = await link.send("drive", dist_m=3.0, speed=0.5)
                        assert isinstance(await link.reply(), Ack)
                        await asyncio.sleep(0.2)
                        stopping = await link.send("stop")
                        seen = [await link.reply() for _ in range(3)]
                        by_id = {m.cmd_id: m for m in seen if not isinstance(m, Ack)}
                        assert isinstance(by_id[stopping], Done)
                        assert isinstance(by_id[driving], Done), "a stopped drive still dones"
                        assert by_id[driving].result["pose"]["x"] < 3.0
                    finally:
                        beat.cancel()
                        link.close()
        finally:
            b.close()

    asyncio.run(asyncio.wait_for(go(), 40.0))


def test_estop_mid_drive_aborts_with_e_estop_over_the_wire():
    async def go():
        b = backend(motors="sim")
        try:
            async with ws_server.serve_rover(b, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                async with connect(f"ws://127.0.0.1:{port}/rover", ping_interval=None) as ws:
                    link = Link(ws)
                    beat = link.heartbeats()
                    try:
                        driving = await link.send("drive", dist_m=3.0, speed=0.5)
                        assert isinstance(await link.reply(), Ack)
                        await asyncio.sleep(0.2)
                        stopping = await link.send("estop")
                        seen = [await link.reply() for _ in range(3)]
                        by_id = {m.cmd_id: m for m in seen if not isinstance(m, Ack)}
                        assert by_id[stopping].result == {"latched": True}
                        assert isinstance(by_id[driving], Err)
                        assert by_id[driving].code == "E_ESTOP"
                        # Latched: a further drive is refused outright.
                        assert (await link.run("drive", dist_m=0.1, speed=0.5)).code == "E_ESTOP"
                    finally:
                        beat.cancel()
                        link.close()
        finally:
            b.close()

    asyncio.run(asyncio.wait_for(go(), 40.0))
