"""Tests for rover/rover/ws_server.py, the protocol layer the real Pi and the mock share.

rover/tests/test_protocol.py already drives this layer hard through fake_rover's `Sim`. This
file covers what a pure-asyncio simulator can't reach:

  * a backend whose long ops block in a *real* thread, which is how the Pi behaves
  * `request_stop` actually being called when a deadline or a watchdog cut fires, since
    cancelling an await does not stop a worker thread
  * events emitted from a worker thread arriving at the client
  * a second connection (doc 00 section 6.2's estop side-channel) closing without cutting
    the primary connection's motion
  * the dependency floor: importing this module must not drag in cv2/numpy/gpiozero
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from marsrover_shared.schemas import (  # noqa: E402
    Ack,
    Cmd,
    Done,
    Err,
    ErrCode,
    Event,
    Imu,
    Pose,
    RoverState,
    ScoopState,
    Telemetry,
    parse_rover_message,
)
from rover import ws_server  # noqa: E402
from rover.ws_server import EventBus, OpError, OpSlot, cmd_id_of, first_error  # noqa: E402

RUN_ID = "r_20261024_101500"


# --- a backend that blocks in a real thread ------------------------------------------------


class StubBackend:
    """Minimal `Backend`. Long ops sleep in a worker thread, like the Pi's blocking GPIO."""

    def __init__(self, *, block_s: float = 0.0) -> None:
        self.block_s = block_s
        self.run_id = ""
        self._state: RoverState = "IDLE"
        self._estop = False
        self.bus = EventBus()
        self.pool_calls: list[str] = []
        self.stop_requests: list[ErrCode | None] = []
        self.heartbeats = 0
        self.cut: ErrCode | None = None  # set by a test to make watchdog_tick fire once
        self._stop = threading.Event()
        self.pose = Pose(x=0.0, y=0.0, heading_deg=0.0, source="dr", sigma_xy_m=0.01)
        self.closed = False

    # --- observable state ---
    def telemetry(self) -> Telemetry:
        return Telemetry(
            run_id=self.run_id,
            pose=self.pose,
            state=self._state,
            estop=self._estop,
            imu=Imu(ax=0.0, ay=0.0, az=9.79, gz_dps=0.0),
            scoop=ScoopState(pose="STOW", us=[1500, 1500]),
            cpu_temp_c=40.0,
            queue_len=0,
            uplink_backlog=0,
        )

    @property
    def state(self) -> RoverState:
        return self._state

    @property
    def estopped(self) -> bool:
        return self._estop

    def attach(self, q: asyncio.Queue[Event]) -> None:
        self.bus.attach(q)

    def detach(self, q: asyncio.Queue[Event]) -> None:
        self.bus.detach(q)

    # --- immediate ops ---
    def start_run(self, run_id: str) -> dict[str, Any]:
        self.run_id = run_id
        return {"ok": True}

    def set_pose(self, x: float, y: float, heading_deg: float, source: str) -> dict[str, Any]:
        self.pose = Pose(x=x, y=y, heading_deg=heading_deg, source=source)
        return {"pose": self.pose.model_dump()}

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        return {"pose": self.pose.model_dump()}

    def estop(self) -> dict[str, Any]:
        self._estop, self._state = True, "ESTOP"
        self._stop.set()
        return {"latched": True}

    def clear_estop(self) -> dict[str, Any]:
        self._estop, self._state = False, "IDLE"
        self._stop.clear()
        return {"latched": False}

    def snapshot(self) -> dict[str, Any]:
        return {"url": ws_server.SNAPSHOT_URL}

    # --- long ops: all of them block in a thread ---
    def _blocking(self, name: str) -> dict[str, Any]:
        """Runs on a worker thread. Polls the cooperative stop, exactly like Motion.drive."""
        self.pool_calls.append(name)
        deadline = time.monotonic() + self.block_s
        while time.monotonic() < deadline:
            if self._stop.is_set():
                break
            time.sleep(0.01)
        return {"op": name, "pose": self.pose.model_dump()}

    async def _offload(self, name: str, state: RoverState) -> dict[str, Any]:
        self._stop.clear()
        self._state = state
        try:
            return await asyncio.to_thread(self._blocking, name)
        finally:
            if not self._estop:
                self._state = "IDLE"

    async def drive(self, dist_m: float, speed: float) -> dict[str, Any]:
        return await self._offload("drive", "MOVING")

    async def turn(self, deg: float, speed: float) -> dict[str, Any]:
        return await self._offload("turn", "MOVING")

    async def station(self, station_id: str, n_views: int, step_deg: float) -> dict[str, Any]:
        return await self._offload("station", "STATION")

    async def approach(self, cand_id: str, x: float, y: float) -> dict[str, Any]:
        return await self._offload("approach", "APPROACH")

    async def scoop(self, sample_id: str) -> dict[str, Any]:
        raise OpError("E_SCOOP", "stub refuses to scoop")

    async def dump(self) -> dict[str, Any]:
        return await self._offload("dump", "SCOOPING")

    async def scoop_pose(self, pose: str) -> dict[str, Any]:
        return {"ok": True}

    # --- safety hooks ---
    def on_heartbeat(self) -> None:
        self.heartbeats += 1

    def watchdog_tick(self) -> ErrCode | None:
        code, self.cut = self.cut, None
        return code

    def request_stop(self, code: ErrCode | None = None) -> None:
        self.stop_requests.append(code)
        self._stop.set()

    def close(self) -> None:
        self.closed = True


# --- client harness ------------------------------------------------------------------------


class Client:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.replies: asyncio.Queue = asyncio.Queue()
        self.events: list[Event] = []
        self.telemetry: list[Telemetry] = []
        self._n = 0
        self._pump = asyncio.create_task(self._read())

    async def _read(self) -> None:
        with contextlib.suppress(Exception):
            async for raw in self.ws:
                msg = parse_rover_message(raw)
                if isinstance(msg, Telemetry):
                    self.telemetry.append(msg)
                elif isinstance(msg, Event):
                    self.events.append(msg)
                else:
                    self.replies.put_nowait(msg)

    def close(self) -> None:
        self._pump.cancel()

    async def send(self, op: str, **args) -> str:
        self._n += 1
        cmd_id = f"q_{self._n:06d}"
        await self.ws.send(Cmd(cmd_id=cmd_id, op=op, args=args).model_dump_json())
        return cmd_id

    async def reply(self, timeout: float = 5.0):
        return await asyncio.wait_for(self.replies.get(), timeout)

    async def run(self, op: str, timeout: float = 5.0, **args):
        cmd_id = await self.send(op, **args)
        ack = await self.reply(timeout)
        assert isinstance(ack, Ack) and ack.cmd_id == cmd_id, f"expected ack, got {ack!r}"
        final = await self.reply(timeout)
        assert final.cmd_id == cmd_id
        return final


@contextlib.asynccontextmanager
async def served(backend: StubBackend):
    """The real ws_server on a free port, with one client attached. Yields (client, port)."""
    async with ws_server.serve_rover(backend, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with connect(f"ws://127.0.0.1:{port}/rover", ping_interval=None) as ws:
            client = Client(ws)
            try:
                yield client, port
            finally:
                client.close()


def run(coro, timeout: float = 25.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


# --- the dependency floor ------------------------------------------------------------------


def test_importing_the_protocol_layer_pulls_in_no_hardware_deps():
    """fake_rover must stay runnable with only pydantic/websockets/httpx/pillow installed."""
    code = (
        f"import sys; sys.path.insert(0, r'{ROOT / 'rover'}');"
        f"sys.path.insert(0, r'{ROOT / 'shared' / 'py'}');"
        "import rover.ws_server;"
        "bad = [m for m in ('cv2', 'numpy', 'gpiozero', 'lgpio', 'smbus2', 'yaml')"
        " if m in sys.modules];"
        "print(','.join(bad))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=True
    )
    assert out.stdout.strip() == "", f"ws_server imported {out.stdout.strip()}"


def test_contract_constants_match_doc00():
    assert ws_server.ACK_BUDGET_S == 0.5
    assert ws_server.TELEMETRY_HZ == 5.0
    assert ws_server.HEARTBEAT_TIMEOUT_S == 1.0
    assert ws_server.OP_TIMEOUT_S == {"station": 60.0, "approach": 45.0, "scoop": 30.0}
    assert ws_server.PATH == "/rover"
    assert ws_server.ESTOP_EXEMPT_OPS == {"heartbeat", "clear_estop", "set_pose", "snapshot"}
    # SCOOPING is deliberately not a moving state: it is a servo sequence, not driving.
    assert ws_server.MOVING_STATES == {"MOVING", "STATION", "APPROACH"}


# --- units ---------------------------------------------------------------------------------


def test_cmd_id_of_recovers_what_it_can():
    assert cmd_id_of('{"cmd_id": "q_000007", "op": "drive"}') == "q_000007"
    assert cmd_id_of(b'{"cmd_id": "q_000007"}') == "q_000007"
    assert cmd_id_of("{not json") is None
    assert cmd_id_of('{"op": "drive"}') is None
    assert cmd_id_of('{"cmd_id": 7}') is None
    assert cmd_id_of('{"cmd_id": ""}') is None
    assert cmd_id_of("[1, 2]") is None


def test_first_error_is_one_short_line():
    with pytest.raises(ValueError) as exc:
        Cmd.model_validate_json(json.dumps({"cmd_id": "q_1", "op": "drive", "args": {}}))
    line = first_error(exc.value)
    assert "\n" not in line and line


def test_op_slot_holds_one_op_and_forgets_its_abort_reason():
    slot = OpSlot()
    owner = object()
    assert not slot.busy and slot.owner is None
    slot.begin(owner)
    assert slot.busy and slot.owner is owner
    # No task yet: an abort must not leave a reason behind for the *next* op to pick up.
    slot.abort("E_ESTOP")
    assert slot.take_abort() is None
    slot.end()
    assert not slot.busy and slot.owner is None


def test_event_bus_attach_detach_and_emit():
    async def go():
        bus = EventBus()
        q: asyncio.Queue[Event] = asyncio.Queue()
        bus.attach(q)
        bus.emit("tilt", {"tilt_deg": 40.0})
        event = await asyncio.wait_for(q.get(), 1.0)
        assert event.kind == "tilt" and event.detail["tilt_deg"] == 40.0
        bus.detach(q)
        bus.emit("bump", {})
        assert q.empty()
        bus.detach(q)  # detaching twice must not raise

    run(go())


def test_event_bus_delivers_from_a_worker_thread():
    """asyncio.Queue.put_nowait is not thread-safe; the real backend emits off-loop."""

    async def go():
        bus = EventBus()
        q: asyncio.Queue[Event] = asyncio.Queue()
        bus.attach(q)
        await asyncio.to_thread(bus.emit, "upload_retry", {"from": "worker"})
        event = await asyncio.wait_for(q.get(), 2.0)
        assert event.detail == {"from": "worker"}

    run(go())


# --- dispatch against a thread-blocking backend --------------------------------------------


def test_ack_then_done_for_a_blocking_op():
    async def go():
        backend = StubBackend(block_s=0.05)
        async with served(backend) as (client, _):
            final = await client.run("drive", dist_m=0.3, speed=0.5)
            assert isinstance(final, Done), final
            assert final.result["op"] == "drive"
            assert backend.pool_calls == ["drive"]

    run(go())


def test_ack_arrives_within_the_budget_even_while_a_thread_blocks():
    async def go():
        backend = StubBackend(block_s=1.0)
        async with served(backend) as (client, _):
            await client.send("drive", dist_m=1.0, speed=0.5)
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            ack = await client.reply(ws_server.ACK_BUDGET_S)
            assert isinstance(ack, Ack)
            assert loop.time() - t0 < ws_server.ACK_BUDGET_S
            backend.request_stop()  # let the worker finish so teardown is quick
            await client.reply()

    run(go())


def test_a_second_long_op_while_a_thread_blocks_gets_e_busy():
    async def go():
        backend = StubBackend(block_s=0.4)
        async with served(backend) as (client, _):
            first = await client.send("drive", dist_m=1.0, speed=0.5)
            second = await client.send("turn", deg=90.0, speed=0.5)
            seen = [await client.reply() for _ in range(4)]
            errs = [m for m in seen if isinstance(m, Err)]
            dones = [m for m in seen if isinstance(m, Done)]
            assert [e.cmd_id for e in errs] == [second]
            assert errs[0].code == "E_BUSY"
            assert [d.cmd_id for d in dones] == [first]
            assert backend.pool_calls == ["drive"], "the refused op must never reach the backend"

    run(go())


def test_a_deadline_overrun_requests_a_stop_as_well_as_replying(monkeypatch):
    """Cancelling the await does not stop a worker thread, so the backend must be told."""
    monkeypatch.setitem(ws_server.OP_TIMEOUT_S, "station", 0.05)

    async def go():
        backend = StubBackend(block_s=2.0)
        async with served(backend) as (client, _):
            final = await client.run("station", timeout=5.0, station_id="s_0001")
            assert isinstance(final, Err) and final.code == "E_TIMEOUT"
            assert "E_TIMEOUT" in backend.stop_requests, backend.stop_requests

    run(go())


def test_estop_latches_aborts_the_blocking_op_and_gates_the_rest():
    async def go():
        backend = StubBackend(block_s=2.0)
        async with served(backend) as (client, _):
            driving = await client.send("drive", dist_m=2.0, speed=0.5)
            assert isinstance(await client.reply(), Ack)
            await asyncio.sleep(0.05)
            stopping = await client.send("estop")
            seen = [await client.reply() for _ in range(3)]
            by_id = {m.cmd_id: m for m in seen if not isinstance(m, Ack)}
            assert by_id[stopping].result == {"latched": True}
            assert isinstance(by_id[driving], Err) and by_id[driving].code == "E_ESTOP"
            # Latched: everything but the exempt ops is refused.
            assert (await client.run("drive", dist_m=0.1, speed=0.5)).code == "E_ESTOP"
            assert (await client.run("dump")).code == "E_ESTOP"
            assert isinstance(await client.run("snapshot"), Done)
            assert isinstance(await client.run("clear_estop"), Done)
            assert isinstance(await client.run("scoop_pose", pose="STOW"), Done)

    run(go())


def test_a_watchdog_cut_aborts_the_blocking_op_once():
    async def go():
        backend = StubBackend(block_s=2.0)
        async with served(backend) as (client, _):
            await client.send("drive", dist_m=2.0, speed=0.5)
            assert isinstance(await client.reply(), Ack)
            await asyncio.sleep(0.05)
            backend.cut = "E_TIMEOUT"  # one tick's worth of watchdog verdict
            final = await client.reply(timeout=3.0)
            assert isinstance(final, Err) and final.code == "E_TIMEOUT"
            assert client.replies.empty(), "exactly one reply per command"

    run(go())


def test_an_op_error_from_the_backend_becomes_an_err_frame():
    async def go():
        async with served(StubBackend()) as (client, _):
            final = await client.run("scoop", sample_id="a_0001")
            assert isinstance(final, Err) and final.code == "E_SCOOP"
            assert "refuses" in final.msg

    run(go())


def test_bad_args_and_missing_cmd_id():
    async def go():
        async with served(StubBackend()) as (client, _):
            await client.ws.send(
                json.dumps({"type": "cmd", "src": "brain", "cmd_id": "q_000042", "op": "fly"})
            )
            err = await client.reply(ws_server.ACK_BUDGET_S)
            assert isinstance(err, Err) and err.code == "E_BAD_ARGS"
            assert err.cmd_id == "q_000042"
            # No cmd_id to answer -> dropped, not guessed at.
            await client.ws.send(json.dumps({"type": "cmd", "src": "brain", "op": "drive"}))
            await client.ws.send("{not json")
            await asyncio.sleep(ws_server.ACK_BUDGET_S)
            assert client.replies.empty()

    run(go())


def test_heartbeat_reaches_the_backend_and_is_never_acked():
    async def go():
        backend = StubBackend()
        async with served(backend) as (client, _):
            before = backend.heartbeats  # the connection itself counts as one
            for _ in range(3):
                await client.send("heartbeat")
            await asyncio.sleep(ws_server.ACK_BUDGET_S)
            assert client.replies.empty()
            assert backend.heartbeats == before + 3

    run(go())


def test_telemetry_flows_at_5hz():
    async def go():
        backend = StubBackend()
        async with served(backend) as (client, _):
            await client.run("start_run", run_id=RUN_ID)
            client.telemetry.clear()
            await asyncio.sleep(1.0)
            assert len(client.telemetry) >= 4
            assert all(t.run_id == RUN_ID for t in client.telemetry)

    run(go())


def test_a_worker_thread_event_reaches_the_client():
    async def go():
        backend = StubBackend()
        async with served(backend) as (client, _):
            await asyncio.to_thread(backend.bus.emit, "upload_retry", {"backlog": 2})
            for _ in range(50):
                if client.events:
                    break
                await asyncio.sleep(0.02)
            assert [e.kind for e in client.events] == ["upload_retry"]
            assert client.events[0].detail == {"backlog": 2}

    run(go())


def test_a_second_connection_closing_does_not_stop_the_first_ones_motion():
    """Doc 00 section 6.2: `estop` may arrive on a second socket. Its close must not cut a drive."""

    async def go():
        backend = StubBackend(block_s=0.6)
        async with served(backend) as (client, port):
            driving = await client.send("drive", dist_m=1.0, speed=0.5)
            assert isinstance(await client.reply(), Ack)
            # A short-lived second connection that does nothing, then goes away.
            async with connect(f"ws://127.0.0.1:{port}/rover", ping_interval=None) as side:
                await side.send(Cmd(cmd_id="q_900001", op="heartbeat").model_dump_json())
            await asyncio.sleep(0.05)
            assert backend.stop_requests == [], "a side connection closing cut the primary drive"
            final = await client.reply(timeout=3.0)
            assert isinstance(final, Done) and final.cmd_id == driving

    run(go())


def test_the_owning_connection_closing_stops_the_motion():
    async def go():
        backend = StubBackend(block_s=2.0)
        async with ws_server.serve_rover(backend, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}/rover", ping_interval=None) as ws:
                client = Client(ws)
                await client.send("drive", dist_m=2.0, speed=0.5)
                assert isinstance(await client.reply(), Ack)
                await asyncio.sleep(0.05)
                client.close()
            for _ in range(100):  # the close is asynchronous
                if backend.stop_requests:
                    break
                await asyncio.sleep(0.02)
            assert backend.stop_requests, "the brain went away and the motors kept going"

    run(go())


def test_a_wrong_path_is_refused():
    async def go():
        async with ws_server.serve_rover(StubBackend(), "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            with pytest.raises(Exception):  # noqa: B017 - any close/handshake failure will do
                async with connect(f"ws://127.0.0.1:{port}/nope", ping_interval=None) as ws:
                    await ws.recv()

    run(go())
