"""WebSocket client to the rover runtime (doc 00 §6.2): commands, heartbeat, telemetry.

- cmd(): send, wait for ack (≤ 0.5 s), then done/err within the op's timeout.
- A heartbeat goes out every 0.25 s while connected; the Pi stops its motors after 1 s
  without one while moving, so the heartbeat never waits on anything else.
- estop() bypasses everything: it goes out on the main socket immediately and, if that
  fails, on a second short-lived connection.
- Reconnects forever with a short backoff; commands in flight fail with E_LINK.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

import websockets
from websockets.asyncio.client import ClientConnection, connect

from marsrover_shared.ids import IdGen
from marsrover_shared.schemas import Ack, Cmd, Done, Err, Event, Telemetry, parse_rover_message

log = logging.getLogger(__name__)

ACK_TIMEOUT_S = 0.5
HEARTBEAT_S = 0.25
# done timeouts per op (doc 00 §4); motion gets a generous default.
DONE_TIMEOUT_S = {"station": 60.0, "approach": 45.0, "scoop": 30.0}
DEFAULT_DONE_TIMEOUT_S = 30.0


class RoverError(Exception):
    def __init__(self, code: str, msg: str = "") -> None:
        super().__init__(f"{code}: {msg}" if msg else code)
        self.code = code
        self.msg = msg


class RoverLink:
    def __init__(self, url: str = "ws://192.168.50.20:8765/rover") -> None:
        self.url = url
        self.on_telemetry: Callable[[Telemetry], None] = lambda t: None
        self.on_event: Callable[[Event], None] = lambda e: None
        self.on_connection: Callable[[bool], None] = lambda up: None
        self.last_telemetry: Telemetry | None = None
        self._ws: ClientConnection | None = None
        self._ids = IdGen()
        # cmd_id -> (ack future, done future)
        self._pending: dict[str, tuple[asyncio.Future, asyncio.Future]] = {}
        self._task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    # --- lifecycle -----------------------------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="rover-link")

    async def stop(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(websockets.WebSocketException):
                await self._ws.close()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        backoff = 0.2
        while True:
            try:
                async with connect(self.url, open_timeout=2, ping_interval=None) as ws:
                    self._ws, backoff = ws, 0.2
                    self.on_connection(True)
                    log.info("rover link up: %s", self.url)
                    hb = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for raw in ws:
                            self._dispatch(raw)
                    finally:
                        hb.cancel()
            except (TimeoutError, OSError, websockets.WebSocketException) as e:
                log.debug("rover link: %s", e)
            if self._ws is not None:
                log.warning("rover link down")
                self._ws = None
                self.on_connection(False)
            self._fail_pending("E_LINK", "rover link lost")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 2.0)

    async def _heartbeat(self, ws: ClientConnection) -> None:
        while True:
            with contextlib.suppress(websockets.WebSocketException):
                await ws.send(Cmd(cmd_id=self._ids.next("q"), op="heartbeat").model_dump_json())
            await asyncio.sleep(HEARTBEAT_S)

    # --- incoming ------------------------------------------------------------------------------
    def _dispatch(self, raw: str | bytes) -> None:
        try:
            msg = parse_rover_message(raw)
        except ValueError as e:
            log.warning("rover sent an invalid message: %s", e)
            return
        if isinstance(msg, Telemetry):
            self.last_telemetry = msg
            self.on_telemetry(msg)
        elif isinstance(msg, Event):
            self.on_event(msg)
        elif isinstance(msg, Ack | Done | Err):
            futs = self._pending.get(msg.cmd_id)
            if not futs:
                return
            ack, done = futs
            if not ack.done():
                ack.set_result(True)
            if isinstance(msg, Done) and not done.done():
                done.set_result(msg.result)
            elif isinstance(msg, Err) and not done.done():
                done.set_exception(RoverError(msg.code, msg.msg))

    def _fail_pending(self, code: str, msg: str) -> None:
        for ack, done in self._pending.values():
            for fut in (ack, done):
                if not fut.done():
                    fut.set_exception(RoverError(code, msg))
        self._pending.clear()

    # --- outgoing ------------------------------------------------------------------------------
    async def cmd(self, op: str, args: dict | None = None, timeout_s: float | None = None) -> dict:
        """Run one op; returns done.result or raises RoverError(code)."""
        ws = self._ws
        if ws is None:
            raise RoverError("E_LINK", "rover not connected")
        cmd = Cmd(cmd_id=self._ids.next("q"), op=op, args=args or {})
        loop = asyncio.get_running_loop()
        ack, done = loop.create_future(), loop.create_future()
        self._pending[cmd.cmd_id] = (ack, done)
        try:
            await ws.send(cmd.model_dump_json())
            try:
                await asyncio.wait_for(asyncio.shield(ack), ACK_TIMEOUT_S)
            except TimeoutError:
                raise RoverError("E_TIMEOUT", f"{op}: no ack within {ACK_TIMEOUT_S} s") from None
            limit = timeout_s or DONE_TIMEOUT_S.get(op, DEFAULT_DONE_TIMEOUT_S)
            try:
                return await asyncio.wait_for(asyncio.shield(done), limit)
            except TimeoutError:
                raise RoverError("E_TIMEOUT", f"{op}: not done within {limit:g} s") from None
        finally:
            self._pending.pop(cmd.cmd_id, None)

    async def estop(self) -> None:
        """Latch the rover's E-STOP now. Never queued, never waits on other commands."""
        msg = Cmd(cmd_id=self._ids.next("q"), op="estop").model_dump_json()
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.send(msg), 0.2)
                return
            except (TimeoutError, websockets.WebSocketException):
                pass
        # Second path: a fresh connection just for this message (doc 00 §6.2).
        async with connect(self.url, open_timeout=1, ping_interval=None) as ws:
            await ws.send(msg)
