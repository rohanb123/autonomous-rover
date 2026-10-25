"""HTTP :8080 on the Pi: /snapshot.jpg and /stream.mjpg (doc 00 section 3, debug only).

Both come off the camera's `lores` stream, never `main`: a station's `grab()` flushes and
waits on `main`, and a browser tab must not be able to delay a keyframe or steal one.

    /stream.mjpg   multipart/x-mixed-replace. On the Pi 5 the ISP encodes MJPEG in hardware
                   (Camera.stream_mjpeg); elsewhere a grab_lores() + jpeg() loop at STREAM_FPS.
                   Either producer runs only while someone is watching.
    /snapshot.jpg  the stream's latest frame if it is fresh, else one lores grab.
    /health        {"ok": true, "camera": bool, "viewers": int}

Runs on the rover runtime's own asyncio loop (see `serve`), so there is one process, one
camera claim (Picamera2 can only be opened once) and one shutdown path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse, StreamingResponse

from rover.camera import Camera, CameraError

log = logging.getLogger("rover.snapshot")

PORT = 8080  # doc 00 section 3
STREAM_FPS = 10.0
FRESH_S = 0.5  # a streamed frame younger than this answers /snapshot.jpg directly
BOUNDARY = "frame"


class FrameHub:
    """The newest JPEG plus a sequence number. Written from any thread, read from the loop.

    `write()` is the file-like interface picamera2's FileOutput calls once per encoded frame,
    which is why the hardware encoder can feed this directly.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: bytes = b""
        self._seq = 0
        self._at = 0.0

    def write(self, buf: bytes) -> int:
        with self._lock:
            self._frame = bytes(buf)
            self._seq += 1
            self._at = time.monotonic()
        return len(buf)

    def flush(self) -> None:  # file-like; FileOutput may call it
        pass

    def latest(self) -> tuple[int, bytes, float]:
        """(seq, jpeg, age_s); seq 0 means nothing yet."""
        with self._lock:
            return self._seq, self._frame, time.monotonic() - self._at


class Streamer:
    """Starts a producer for the first viewer and stops it after the last one leaves."""

    def __init__(self, camera: Camera, fps: float = STREAM_FPS) -> None:
        self.camera = camera
        self.period = 1.0 / fps
        self.hub = FrameHub()
        self.viewers = 0
        self._hardware = False
        self._task: asyncio.Task | None = None

    def _grab_jpeg(self) -> bytes:
        return self.camera.jpeg(self.camera.grab_lores())

    async def _software_loop(self) -> None:
        while True:
            t0 = time.monotonic()
            try:
                self.hub.write(await asyncio.to_thread(self._grab_jpeg))
            except CameraError as e:
                log.warning("stream grab failed: %s", e)
            await asyncio.sleep(max(0.0, self.period - (time.monotonic() - t0)))

    def _start(self) -> None:
        self._hardware = self.camera.stream_mjpeg(self.hub)
        if not self._hardware:
            self._task = asyncio.create_task(self._software_loop(), name="mjpeg-sw")
        log.info("stream started (%s)", "hardware MJPEG" if self._hardware else "software")

    def _stop(self) -> None:
        if self._hardware:
            self.camera.stop_stream()
            self._hardware = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        log.info("stream stopped (no viewers)")

    @contextlib.contextmanager
    def viewer(self) -> Iterator[None]:
        self.viewers += 1
        if self.viewers == 1:
            self._start()
        try:
            yield
        finally:
            self.viewers -= 1
            if self.viewers == 0:
                self._stop()

    async def frames(self) -> AsyncIterator[bytes]:
        """Each new frame once, as a multipart chunk. Polls the hub at twice the frame rate."""
        with self.viewer():
            seen = 0
            while True:
                seq, jpeg, _ = self.hub.latest()
                if seq != seen and jpeg:
                    seen = seq
                    yield (
                        f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpeg)}\r\n\r\n"
                    ).encode() + jpeg + b"\r\n"
                await asyncio.sleep(self.period / 2)

    async def snapshot(self) -> bytes:
        seq, jpeg, age = self.hub.latest()
        if seq and jpeg and age < FRESH_S:
            return jpeg
        return await asyncio.to_thread(self._grab_jpeg)

    def close(self) -> None:
        if self.viewers:
            self.viewers = 0
            self._stop()


def make_app(camera: Camera, fps: float = STREAM_FPS) -> FastAPI:
    streamer = Streamer(camera, fps)
    app = FastAPI(title="rover snapshot", docs_url=None, redoc_url=None)
    app.state.streamer = streamer

    def unavailable(why: str) -> JSONResponse:
        return JSONResponse({"ok": False, "error": why}, status_code=503)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "camera": camera.present, "viewers": streamer.viewers}

    @app.get("/snapshot.jpg", response_model=None)
    async def snapshot() -> Response:
        if not camera.present:
            return unavailable("no camera attached")
        try:
            jpeg = await streamer.snapshot()
        except CameraError as e:
            return unavailable(str(e))
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/stream.mjpg", response_model=None)
    async def stream() -> Response:
        if not camera.present:
            return unavailable("no camera attached")
        return StreamingResponse(
            streamer.frames(),
            media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            headers={"Cache-Control": "no-store"},
        )

    return app


async def serve(camera: Camera, host: str = "0.0.0.0", port: int = PORT) -> None:
    """Serve until cancelled, on the caller's loop. The runtime owns signals, not uvicorn."""
    import uvicorn

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:  # uvicorn >= 0.29
            yield

        def install_signal_handlers(self) -> None:  # older uvicorn
            pass

    app = make_app(camera)
    server = _Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    try:
        await server.serve()
    finally:
        server.should_exit = True
        app.state.streamer.close()
