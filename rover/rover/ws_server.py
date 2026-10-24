"""The brain <-> rover WebSocket protocol: `ws://<host>:8765/rover` (doc 00 section 6.2).

This is the *only* implementation of the protocol in the repo. It knows nothing about motors,
cameras or simulation: everything behind `Backend` is injected, so the same dispatch,
telemetry loop and heartbeat watchdog serve both the real Pi (`rover.main.HardwareBackend`)
and the mock (`mocks/fake_rover.py`'s `Sim`). Duplicating it would be the obvious place for
the brain and the rover to drift apart, which is docs/05 R12.

Deliberately dependency-light: `websockets` + `pydantic` + stdlib, nothing else. `cv2`,
`numpy`, `gpiozero` and friends must never reach this module, or fake_rover stops being
runnable on a teammate's laptop. `rover/tests/test_ws_server.py` asserts that.

What the protocol layer owns:
  * parsing every frame with `Cmd.model_validate_json`, and `E_BAD_ARGS` when that fails
  * `ack` within 0.5 s, then exactly one `done` or `err`
  * one long op at a time; a second gets `E_BUSY`
  * per-op deadlines (station 60 s, approach 45 s, scoop 30 s) -> `E_TIMEOUT`
  * 5 Hz telemetry, 20 Hz watchdog
  * the E-STOP latch gate: while latched, only clear_estop/heartbeat/set_pose/snapshot run

What the backend owns: pose, state, imagery, uploads, and whether a cutoff just fired.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

import websockets
from websockets.asyncio.server import ServerConnection, serve

from marsrover_shared.schemas import (
    Ack,
    Cmd,
    Done,
    Err,
    ErrCode,
    Event,
    RoverState,
    Telemetry,
)

log = logging.getLogger("rover.ws_server")

# --- contract constants (doc 00 section 4; these are the contract, don't tune them) --------

PATH = "/rover"
ACK_BUDGET_S = 0.5  # an ack must be on the wire within this
TELEMETRY_HZ = 5.0
WATCHDOG_HZ = 20.0
HEARTBEAT_TIMEOUT_S = 1.0  # no heartbeat for this long *while moving* -> stop the motors
# Per-op done deadlines. Read at dispatch time so a test can shrink them.
OP_TIMEOUT_S: dict[str, float] = {"station": 60.0, "approach": 45.0, "scoop": 30.0}
# Doc 00 section 3: the real Pi serves this on :8080; this layer only hands the URL out.
SNAPSHOT_URL = "http://192.168.50.20:8080/snapshot.jpg"

# Ops answered on the reader loop, so they land even while a long op runs (P2 role doc 9).
IMMEDIATE_OPS = frozenset({"heartbeat", "estop", "stop", "clear_estop", "set_pose", "snapshot"})
# Ops that still work while the E-STOP latch is set; everything else gets E_ESTOP.
ESTOP_EXEMPT_OPS = frozenset({"heartbeat", "clear_estop", "set_pose", "snapshot"})
# States the heartbeat watchdog may cut the motors in. SCOOPING is left out: it is a servo
# sequence with one 4 cm nudge, not driving.
MOVING_STATES: frozenset[RoverState] = frozenset({"MOVING", "STATION", "APPROACH"})


class OpError(Exception):
    """Raised by a backend op to answer with `err` instead of `done`."""

    def __init__(self, code: ErrCode, msg: str = "") -> None:
        super().__init__(f"{code}: {msg}" if msg else code)
        self.code: ErrCode = code
        self.msg = msg


# --- the backend seam ----------------------------------------------------------------------


@runtime_checkable
class Backend(Protocol):
    """Everything the protocol layer needs from a rover, real or simulated.

    Long ops are `async def` so a simulator can just sleep while the real Pi offloads its
    blocking GPIO work to a worker thread. The immediate ops must be sub-millisecond: they
    run on the event loop, which is what makes `estop` land mid-op.
    """

    # --- observable state ---
    def telemetry(self) -> Telemetry: ...
    @property
    def state(self) -> RoverState: ...
    @property
    def estopped(self) -> bool: ...

    # --- event fan-out ---
    def attach(self, queue: asyncio.Queue[Event]) -> None: ...
    def detach(self, queue: asyncio.Queue[Event]) -> None: ...

    # --- immediate ops (loop thread, no awaits) ---
    def start_run(self, run_id: str) -> dict[str, Any]: ...
    def set_pose(self, x: float, y: float, heading_deg: float, source: str) -> dict[str, Any]: ...
    def stop(self) -> dict[str, Any]: ...
    def estop(self) -> dict[str, Any]: ...
    def clear_estop(self) -> dict[str, Any]: ...
    def snapshot(self) -> dict[str, Any]: ...

    # --- long ops ---
    async def drive(self, dist_m: float, speed: float) -> dict[str, Any]: ...
    async def turn(self, deg: float, speed: float) -> dict[str, Any]: ...
    async def station(self, station_id: str, n_views: int, step_deg: float) -> dict[str, Any]: ...
    async def approach(self, cand_id: str, x: float, y: float) -> dict[str, Any]: ...
    async def scoop(self, sample_id: str) -> dict[str, Any]: ...
    async def dump(self) -> dict[str, Any]: ...
    async def scoop_pose(self, pose: str) -> dict[str, Any]: ...

    # --- safety hooks ---
    def on_heartbeat(self) -> None: ...
    def watchdog_tick(self) -> ErrCode | None: ...
    def request_stop(self, code: ErrCode | None = None) -> None: ...
    def close(self) -> None: ...


# --- event fan-out -------------------------------------------------------------------------


class EventBus:
    """Fans `Event`s out to every connected session. Safe to call from a worker thread."""

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[Event]] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach(self, queue: asyncio.Queue[Event]) -> None:
        self._queues.append(queue)
        if self._loop is None:
            with contextlib.suppress(RuntimeError):
                self._loop = asyncio.get_running_loop()

    def detach(self, queue: asyncio.Queue[Event]) -> None:
        with contextlib.suppress(ValueError):
            self._queues.remove(queue)

    def emit(self, kind: str, detail: dict[str, Any]) -> None:
        event = Event(kind=kind, detail=detail)
        for queue in list(self._queues):
            self._deliver(queue, event)

    def _deliver(self, queue: asyncio.Queue[Event], event: Event) -> None:
        # asyncio.Queue.put_nowait is not thread-safe, and the real backend emits from its
        # worker thread (an upload retry, a tilt cut). Hop to the loop when we're not on it.
        loop = self._loop
        try:
            on_loop = asyncio.get_running_loop() is loop
        except RuntimeError:
            on_loop = False
        if loop is None or on_loop:
            queue.put_nowait(event)
            return
        with contextlib.suppress(RuntimeError):  # loop already closed during shutdown
            loop.call_soon_threadsafe(queue.put_nowait, event)


# --- the one-op-at-a-time slot -------------------------------------------------------------


class OpSlot:
    """One rover, one long op. Shared across connections, since there is only one rover."""

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self._abort: ErrCode | None = None
        self.owner: object | None = None  # the Session that started the op

    @property
    def busy(self) -> bool:
        return self._running

    def begin(self, owner: object) -> None:
        """Claimed synchronously on dispatch, so a racing second op still gets E_BUSY."""
        self._running, self.owner = True, owner

    def set_task(self, task: asyncio.Task) -> None:
        self._task = task

    def end(self) -> None:
        self._running, self._task, self.owner = False, None, None

    def abort(self, code: ErrCode) -> None:
        """Cut the running op short; it answers `err code` instead of `done`."""
        if self._task is None or self._task.done():
            return  # nothing to cut: don't leave a reason behind for the next op
        self._abort = code
        self._task.cancel()

    def take_abort(self) -> ErrCode | None:
        code, self._abort = self._abort, None
        return code


# --- frame helpers -------------------------------------------------------------------------


def cmd_id_of(raw: str | bytes) -> str | None:
    """Best-effort cmd_id out of a frame that failed validation, so we can answer it."""
    with contextlib.suppress(ValueError, TypeError, AttributeError):
        value = json.loads(raw).get("cmd_id")
        if isinstance(value, str) and value:
            return value
    return None


def first_error(exc: ValueError) -> str:
    """One short line out of a pydantic ValidationError, for err.msg."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        for e in errors():
            loc = ".".join(str(p) for p in e.get("loc", ()))
            return f"{loc}: {e.get('msg', '')}".strip(": ")
    return str(exc).splitlines()[0]


# --- server --------------------------------------------------------------------------------


class RoverServer:
    """State shared by every connection: one backend, one op slot, one session registry."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.slot = OpSlot()
        self.sessions: set[Session] = set()


class Session:
    """One brain connection: reader + ack/dispatch, 5 Hz telemetry, 20 Hz watchdog."""

    def __init__(self, ws: ServerConnection, server: RoverServer) -> None:
        self.ws = ws
        self.server = server
        self.backend = server.backend
        self.events: asyncio.Queue[Event] = asyncio.Queue()
        self._cmds: set[asyncio.Task] = set()

    async def run(self) -> None:
        backend, server = self.backend, self.server
        backend.on_heartbeat()  # a fresh connection starts the watchdog budget over
        backend.attach(self.events)
        server.sessions.add(self)
        loops = [
            asyncio.create_task(self._telemetry_loop(), name="telemetry"),
            asyncio.create_task(self._watchdog_loop(), name="watchdog"),
            asyncio.create_task(self._event_loop(), name="events"),
        ]
        try:
            async for raw in self.ws:
                await self._on_frame(raw)
        finally:
            backend.detach(self.events)
            server.sessions.discard(self)
            # Only halt motion if this connection is the one that started the running op.
            # Doc 00 section 6.2 lets the brain send `estop` down a second socket; when that
            # short-lived connection closes it must not cut the primary connection's drive.
            if server.slot.owner is self or not server.sessions:
                backend.request_stop()
            for t in (*loops, *self._cmds):
                t.cancel()
            await asyncio.gather(*loops, *self._cmds, return_exceptions=True)

    # --- outgoing -------------------------------------------------------------------------

    async def send(self, msg: Ack | Done | Err | Telemetry | Event) -> None:
        with contextlib.suppress(websockets.WebSocketException):
            await self.ws.send(msg.model_dump_json())

    async def _telemetry_loop(self) -> None:
        while True:
            await self.send(self.backend.telemetry())
            await asyncio.sleep(1.0 / TELEMETRY_HZ)

    async def _event_loop(self) -> None:
        while True:
            await self.send(await self.events.get())

    async def _watchdog_loop(self) -> None:
        """20 Hz. The backend decides whether a cutoff fired; we turn that into an abort."""
        while True:
            await asyncio.sleep(1.0 / WATCHDOG_HZ)
            code = self.backend.watchdog_tick()
            if code is not None:
                log.warning("watchdog cut the running op: %s", code)
                self.server.slot.abort(code)

    # --- incoming -------------------------------------------------------------------------

    async def _on_frame(self, raw: str | bytes) -> None:
        try:
            cmd = Cmd.model_validate_json(raw)
        except ValueError as e:
            cmd_id = cmd_id_of(raw)
            log.info("bad cmd (%s): %s", cmd_id, e)
            if cmd_id:  # no cmd_id -> nothing to answer, so drop it
                await self.send(Err(cmd_id=cmd_id, code="E_BAD_ARGS", msg=first_error(e)))
            return
        if cmd.op == "heartbeat":
            self.backend.on_heartbeat()  # no ack, no done (doc 00 section 6.2)
            return
        await self.send(Ack(cmd_id=cmd.cmd_id))
        await self._dispatch(cmd)

    async def _dispatch(self, cmd: Cmd) -> None:
        slot = self.server.slot
        if self.backend.estopped and cmd.op not in ESTOP_EXEMPT_OPS:
            await self.send(Err(cmd_id=cmd.cmd_id, code="E_ESTOP", msg="E-STOP latched"))
            return
        if cmd.op in IMMEDIATE_OPS:
            try:
                result = self._immediate(cmd)
            except OpError as e:
                await self.send(Err(cmd_id=cmd.cmd_id, code=e.code, msg=e.msg))
                return
            await self.send(Done(cmd_id=cmd.cmd_id, result=result))
            return
        if slot.busy:
            await self.send(Err(cmd_id=cmd.cmd_id, code="E_BUSY", msg=f"busy, {cmd.op} refused"))
            return
        slot.begin(self)  # synchronous, so the next frame already sees us busy
        task = asyncio.create_task(self._run_long(cmd), name=f"cmd:{cmd.cmd_id}")
        self._cmds.add(task)
        task.add_done_callback(self._cmds.discard)

    def _immediate(self, cmd: Cmd) -> dict[str, Any]:
        backend = self.backend
        if cmd.op == "estop":
            # The backend latches and cuts power; aborting the in-flight op is ours to do.
            result = backend.estop()
            self.server.slot.abort("E_ESTOP")
            return result
        if cmd.op == "clear_estop":
            return backend.clear_estop()
        if cmd.op == "stop":
            return backend.stop()
        if cmd.op == "set_pose":
            return backend.set_pose(**cmd.args)
        if cmd.op == "snapshot":
            return backend.snapshot()
        raise OpError("E_INTERNAL", f"{cmd.op} is not an immediate op")

    async def _long(self, cmd: Cmd) -> dict[str, Any]:
        backend = self.backend
        if cmd.op == "start_run":
            return backend.start_run(**cmd.args)
        if cmd.op == "drive":
            return await backend.drive(**cmd.args)
        if cmd.op == "turn":
            return await backend.turn(**cmd.args)
        if cmd.op == "station":
            return await backend.station(**cmd.args)
        if cmd.op == "approach":
            return await backend.approach(**cmd.args)
        if cmd.op == "scoop":
            return await backend.scoop(**cmd.args)
        if cmd.op == "dump":
            return await backend.dump()
        if cmd.op == "scoop_pose":
            return await backend.scoop_pose(**cmd.args)
        raise OpError("E_INTERNAL", f"{cmd.op} has no handler")

    async def _run_long(self, cmd: Cmd) -> None:
        """Hold the op slot, enforce the op's deadline, answer exactly once."""
        slot, backend = self.server.slot, self.backend
        if backend.estopped:  # closes the race between `estop` and a just-dispatched op
            slot.end()
            await self.send(Err(cmd_id=cmd.cmd_id, code="E_ESTOP", msg="E-STOP latched"))
            return
        limit = OP_TIMEOUT_S.get(cmd.op)
        inner = asyncio.create_task(self._long(cmd), name=f"op:{cmd.op}")
        slot.set_task(inner)
        try:
            await self.send(Done(cmd_id=cmd.cmd_id, result=await asyncio.wait_for(inner, limit)))
        except TimeoutError:
            # The awaiting stopped, but a worker thread does not: tell the backend too, or
            # the real rover keeps driving after the brain has been told the op failed.
            backend.request_stop("E_TIMEOUT")
            await self.send(
                Err(cmd_id=cmd.cmd_id, code="E_TIMEOUT", msg=f"{cmd.op} exceeded {limit:g} s")
            )
        except OpError as e:
            await self.send(Err(cmd_id=cmd.cmd_id, code=e.code, msg=e.msg))
        except asyncio.CancelledError:
            code = slot.take_abort()
            if code is None:  # a real shutdown, not an abort
                raise
            await self.send(Err(cmd_id=cmd.cmd_id, code=code, msg=f"{cmd.op} aborted"))
        except Exception as e:  # a bug in the backend, not a contract case
            log.exception("%s failed", cmd.op)
            await self.send(Err(cmd_id=cmd.cmd_id, code="E_INTERNAL", msg=repr(e)))
        finally:
            slot.end()


def make_handler(backend: Backend, path: str = PATH) -> Callable:
    """A `websockets` handler serving `path` for one backend."""
    server = RoverServer(backend)

    async def handler(ws: ServerConnection) -> None:
        got = ws.request.path.split("?")[0] if ws.request is not None else path
        if got != path:  # doc 00 section 3: the endpoint is /rover
            await ws.close(code=1008, reason=f"expected {path}")
            return
        log.info("brain connected: %s", ws.remote_address)
        try:
            await Session(ws, server).run()
        except websockets.WebSocketException as e:
            log.debug("connection closed: %s", e)
        finally:
            log.info("brain disconnected")

    return handler


def serve_rover(backend: Backend, host: str, port: int):
    """`async with serve_rover(...) as server:` -- port 0 picks a free port (tests)."""
    return serve(make_handler(backend), host, port, ping_interval=None)


def heartbeat_expired(last_heartbeat: float, now: float | None = None) -> bool:
    """Doc 00 section 4: no heartbeat for 1.0 s. Shared so both backends agree on it."""
    return (time.monotonic() if now is None else now) - last_heartbeat > HEARTBEAT_TIMEOUT_S
