import asyncio

import pytest

from brain.rover_link import RoverError, RoverLink
from brain.tests.stub_rover import StubRover


async def _up(link: RoverLink, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.02)):
        if link.connected and link.last_telemetry:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("rover link never came up")


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 15))


def test_cmd_done_telemetry_and_heartbeat():
    async def go():
        async with StubRover() as r:
            link = RoverLink(r.url)
            link.start()
            await _up(link)
            res = await link.cmd("drive", {"dist_m": 0.3, "speed": 0.5})
            assert res["pose"]["x"] == pytest.approx(0.3)
            await asyncio.sleep(0.6)
            assert r.heartbeats >= 2 and link.last_telemetry.pose.x == pytest.approx(0.3)
            await link.stop()

    run(go())


def test_error_codes_and_ack_timeout():
    async def go():
        async with StubRover() as r:
            r.fail["sample"] = "E_CAMERA"
            r.no_ack.add("stop")
            link = RoverLink(r.url)
            link.start()
            await _up(link)
            with pytest.raises(RoverError) as e:
                await link.cmd("sample", {"sample_id": "a_0001"})
            assert e.value.code == "E_CAMERA"
            with pytest.raises(RoverError) as e:
                await link.cmd("stop")
            assert e.value.code == "E_TIMEOUT" and "no ack" in e.value.msg
            await link.stop()

    run(go())


def test_estop_latches_until_cleared():
    async def go():
        async with StubRover() as r:
            link = RoverLink(r.url)
            link.start()
            await _up(link)
            await link.estop()
            await asyncio.sleep(0.05)
            with pytest.raises(RoverError) as e:
                await link.cmd("drive", {"dist_m": 0.1, "speed": 0.5})
            assert e.value.code == "E_ESTOP"
            assert (await link.cmd("clear_estop")) == {"latched": False}
            await link.cmd("drive", {"dist_m": 0.1, "speed": 0.5})
            await link.stop()

    run(go())


def test_not_connected_and_link_lost_mid_command():
    async def go():
        link = RoverLink("ws://127.0.0.1:1/rover")
        with pytest.raises(RoverError) as e:
            await link.cmd("stop")
        assert e.value.code == "E_LINK"
        async with StubRover() as r:
            r.hold_station.clear()  # station never finishes
            link = RoverLink(r.url)
            link.start()
            await _up(link)
            pending = asyncio.create_task(link.cmd("station", {"station_id": "s_0001"}))
            await asyncio.sleep(0.1)
        with pytest.raises(RoverError) as e:  # server gone
            await pending
        assert e.value.code == "E_LINK"
        await link.stop()

    run(go())
