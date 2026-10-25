""":8080 /snapshot.jpg, /stream.mjpg, /health -- over ASGI, plus one real uvicorn round trip."""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers  # noqa: E402

from rover import snapshot_server  # noqa: E402
from rover.camera import Camera, NullSource, SyntheticSource  # noqa: E402
from rover.config import parse_calib  # noqa: E402
from rover.snapshot_server import FrameHub, Streamer, make_app  # noqa: E402

pytest.importorskip("cv2")  # jpeg() encodes with OpenCV

CFG = parse_calib(helpers.CALIB_DICT).camera
JPEG_MAGIC = b"\xff\xd8\xff"


def sim_camera() -> Camera:
    return Camera(CFG, SyntheticSource(CFG.width, CFG.height))


def run(coro, timeout: float = 10.0):
    return asyncio.run(asyncio.wait_for(coro, timeout))


async def get(app, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://rover") as client:
        return await client.get(path)


# --- endpoints -----------------------------------------------------------------------------


def test_snapshot_is_a_jpeg():
    r = run(get(make_app(sim_camera()), "/snapshot.jpg"))
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content.startswith(JPEG_MAGIC)


@pytest.mark.parametrize("path", ["/snapshot.jpg", "/stream.mjpg"])
def test_no_camera_is_503_not_a_crash(path):
    r = run(get(make_app(Camera(CFG, NullSource())), path))
    assert r.status_code == 503 and r.json()["ok"] is False


def test_health_reports_camera_and_viewers():
    r = run(get(make_app(sim_camera()), "/health"))
    assert r.json() == {"ok": True, "camera": True, "viewers": 0}


# --- the stream machinery ------------------------------------------------------------------


def test_stream_yields_distinct_boundary_delimited_frames_then_stops_its_producer():
    async def go():
        streamer = Streamer(sim_camera(), fps=50)
        chunks = []
        gen = streamer.frames()
        async for chunk in gen:
            chunks.append(chunk)
            assert streamer.viewers == 1
            if len(chunks) == 3:
                break
        await gen.aclose()
        return streamer, chunks

    streamer, chunks = run(go())
    for chunk in chunks:
        assert chunk.startswith(b"--frame\r\nContent-Type: image/jpeg\r\n")
        assert JPEG_MAGIC in chunk
    assert streamer.viewers == 0 and streamer._task is None  # last viewer left -> stopped


def test_hardware_encoder_path_feeds_the_hub_and_is_stopped():
    """When stream_mjpeg() succeeds, no software loop runs; frames arrive via hub.write()."""

    class HwCamera:
        present = True

        def __init__(self) -> None:
            self.output = None
            self.stopped = False

        def stream_mjpeg(self, output, encoder=None) -> bool:
            self.output = output
            return True

        def stop_stream(self) -> None:
            self.stopped = True

    async def go():
        cam = HwCamera()
        streamer = Streamer(cam, fps=50)  # type: ignore[arg-type]
        gen = streamer.frames()
        first = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.02)
        assert streamer._task is None and cam.output is streamer.hub
        cam.output.write(b"\xff\xd8\xffHW")  # what picamera2's FileOutput does per frame
        chunk = await first
        await gen.aclose()
        return cam, chunk

    cam, chunk = run(go())
    assert chunk.endswith(b"\xff\xd8\xffHW\r\n") and cam.stopped


def test_snapshot_prefers_a_fresh_streamed_frame():
    async def go():
        streamer = Streamer(Camera(CFG, NullSource()))  # a grab would raise
        streamer.hub.write(b"\xff\xd8\xffLIVE")
        return await streamer.snapshot()

    assert run(go()) == b"\xff\xd8\xffLIVE"


def test_hub_sequence_advances_per_write():
    hub = FrameHub()
    assert hub.latest()[0] == 0
    hub.write(b"a")
    hub.write(b"b")
    seq, frame, age = hub.latest()
    assert (seq, frame) == (2, b"b") and age < 1.0


# --- real server ---------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_real_uvicorn_serves_a_live_stream_on_the_runtime_loop():
    async def go():
        port = free_port()
        task = asyncio.create_task(snapshot_server.serve(sim_camera(), "127.0.0.1", port))
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                for _ in range(50):  # wait for the listener
                    try:
                        await client.get(f"http://127.0.0.1:{port}/health")
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(0.05)
                url = f"http://127.0.0.1:{port}/stream.mjpg"
                async with client.stream("GET", url) as r:
                    assert r.headers["content-type"].startswith("multipart/x-mixed-replace")
                    body = b""
                    async for piece in r.aiter_bytes():
                        body += piece
                        if body.count(b"--frame") >= 2:
                            break
            return body
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    body = run(go(), timeout=20.0)
    assert body.count(JPEG_MAGIC) >= 1
