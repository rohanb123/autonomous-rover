import asyncio
import time

from websockets.asyncio.server import serve

from brain.overhead_link import OverheadLink
from marsrover_shared.schemas import OverheadPose


def pose(**kw) -> OverheadPose:
    base = dict(x=0.5, y=-0.2, heading_deg=30.0, marker_id=0, reproj_err_px=0.8, visible=True)
    return OverheadPose(**{**base, **kw})


class PoseServer:
    """Streams `self.pose` at 20 Hz; `self.pose = None` goes quiet without disconnecting."""

    def __init__(self):
        self.pose: OverheadPose | None = pose()

    async def handle(self, ws):
        while True:
            if self.pose is not None:
                await ws.send(self.pose.model_copy(update={"t": time.time()}).model_dump_json())
            try:  # pace at 20 Hz, but return as soon as the client hangs up
                await asyncio.wait_for(ws.wait_closed(), 0.05)
                return
            except TimeoutError:
                pass


def test_latest_usable_and_health():
    async def go():
        srv = PoseServer()
        async with serve(srv.handle, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            link = OverheadLink(f"ws://127.0.0.1:{port}/pose")
            assert link.health == "down" and link.latest() is None
            link.start()
            for _ in range(100):
                if link.latest():
                    break
                await asyncio.sleep(0.02)
            assert link.health == "ok" and link.usable().x == 0.5
            srv.pose = pose(reproj_err_px=2.5)  # too inaccurate to trust
            await asyncio.sleep(0.15)
            assert link.latest() is not None and link.usable() is None
            srv.pose = pose(visible=False)  # marker out of view
            await asyncio.sleep(0.15)
            assert link.usable() is None
            srv.pose = None  # stream goes quiet: stale after 0.5 s
            await asyncio.sleep(0.6)
            assert link.latest() is None and link.usable() is None
            await asyncio.sleep(1.5)
            assert link.health == "degraded"  # connected, nothing fresh
            await link.stop()

    asyncio.run(asyncio.wait_for(go(), 15))


def test_invalid_messages_are_ignored_and_reconnects():
    async def go():
        sent = asyncio.Event()

        async def handle(ws):
            await ws.send('{"type":"overhead_pose","x":"nope"}')
            await ws.send(pose(x=1.0).model_dump_json())
            sent.set()
            await asyncio.sleep(10)

        async with serve(handle, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            link = OverheadLink(f"ws://127.0.0.1:{port}/pose")
            link.start()
            await asyncio.wait_for(sent.wait(), 5)
            await asyncio.sleep(0.1)
            assert link.latest().x == 1.0
        for _ in range(100):  # server gone
            if not link.connected:
                break
            await asyncio.sleep(0.02)
        assert link.health == "down"
        await link.stop()

    asyncio.run(asyncio.wait_for(go(), 15))
