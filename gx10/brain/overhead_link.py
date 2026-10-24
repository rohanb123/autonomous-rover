"""Overhead ArUco pose feed (doc 00 §6.12) → drift correction at stations.

Subscribes to the overhead tracker (P2, gx10/overhead) on ws://…:8300/pose at ~10 Hz and keeps
the latest pose. At each station the FSM asks `usable()`; when it returns a pose, the brain
sends `set_pose(source="aruco")` so dead-reckoning drift is reset before the station's photos.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import websockets
from pydantic import ValidationError
from websockets.asyncio.client import connect

from marsrover_shared.schemas import OverheadPose

log = logging.getLogger(__name__)

MAX_AGE_S = 0.5  # P4 role doc §4: fresh = received in the last 0.5 s
MAX_REPROJ_PX = 2.0  # … and reproj_err_px < 2


class OverheadLink:
    def __init__(self, url: str = "ws://192.168.50.10:8300/pose") -> None:
        self.url = url
        self.connected = False
        self._latest: OverheadPose | None = None
        self._latest_at = 0.0  # time.monotonic() when it arrived
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="overhead-link")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def run(self, url: str | None = None) -> None:
        """Receive poses forever, reconnecting with a short backoff."""
        url = url or self.url
        backoff = 0.2
        while True:
            try:
                async with connect(url, open_timeout=2, ping_interval=None) as ws:
                    self.connected, backoff = True, 0.2
                    log.info("overhead link up: %s", url)
                    async for raw in ws:
                        self._on_message(raw)
            except (OSError, websockets.WebSocketException, TimeoutError) as e:
                log.debug("overhead link: %s", e)
            self.connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 2.0)

    def _on_message(self, raw: str | bytes) -> None:
        try:
            pose = OverheadPose.model_validate_json(raw)
        except ValidationError as e:
            log.warning("overhead sent an invalid pose: %s", e)
            return
        self._latest, self._latest_at = pose, time.monotonic()

    def latest(self, max_age_s: float = MAX_AGE_S) -> OverheadPose | None:
        """The last pose if it arrived within max_age_s, else None."""
        if self._latest is None or time.monotonic() - self._latest_at > max_age_s:
            return None
        return self._latest

    def usable(self) -> OverheadPose | None:
        """A pose good enough to reset dead reckoning: fresh, marker visible, low reprojection."""
        p = self.latest()
        if p is None or not p.visible or p.reproj_err_px >= MAX_REPROJ_PX:
            return None
        return p

    @property
    def health(self) -> str:
        """ok = fresh poses arriving; degraded = connected but stale; down = not connected."""
        if not self.connected:
            return "down"
        return "ok" if self.latest(max_age_s=2.0) else "degraded"
