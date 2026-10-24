import asyncio

import pytest

from brain.fsm import FsmBusy, RunFSM
from brain.rover_link import RoverLink
from brain.tests.stub_rover import StubRover, StubVision
from marsrover_shared.ids import IdGen

MISSION = {
    "mission_id": "m_0001",
    "run_id": "r_test",
    "text": "t",
    "radius_m": 1.5,
    "k": 3,
    "categories": ["float_rock"],
    "sample": True,
    "return_home": True,
    "max_duration_s": 900,
    "mode": "act",
    "assumptions": [],
}
PRIORS = {"float_rock": 1.0, "pebble": 0.5}


class FakeDb:
    def __init__(self):
        self.events, self.tasks, self.cands, self.missions = [], {}, {}, []

    def event(self, kind, msg, level="info", data=None):
        self.events.append((kind, level, msg))

    def mission(self, m, status):
        self.missions.append(status)

    def task(self, tid, run, mission, kind, ref, seq, status, note=""):
        self.tasks[tid] = (kind, ref, seq, status)

    def candidate(self, c, comps):
        self.cands[c.cand_id] = c


async def _setup(r: StubRover, vision=None):
    link = RoverLink(r.url)
    link.start()
    for _ in range(150):
        if link.connected and link.last_telemetry:
            break
        await asyncio.sleep(0.02)
    db = FakeDb()
    return link, db, RunFSM(link, vision or StubVision(), db, PRIORS)


async def _wait(cond, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def test_survey_to_plan():
    async def go():
        async with StubRover() as r:
            link, db, fsm = await _setup(r)
            await fsm.start_mission(MISSION, IdGen())
            assert fsm.state == "PLAN"
            assert r.ops.count("station") == 5 and "drive" in r.ops
            # 5 rocks within reach; the ones seen from 2 stations were merged, not duplicated.
            assert len(db.cands) == 5
            assert any(len(c.prop_ids) > 1 for c in db.cands.values())
            ranked = sorted((c.rank, c.cand_id) for c in db.cands.values() if c.rank > 0)
            assert [rank for rank, _ in ranked] == [1, 2, 3]
            assert all(db.cands[cid].status == "queued" for _, cid in ranked)
            stations = [t for t in db.tasks.values() if t[0] == "station"]
            visits = [t for t in db.tasks.values() if t[0] == "visit"]
            assert len(stations) == 5 and all(t[3] == "done" for t in stations)
            assert len(visits) == 3 and all(t[3] == "pending" for t in visits)
            assert db.missions == ["active"]
            assert any("INVESTIGATE is not built yet" in m for _, _, m in db.events)
            assert all(c.reason and len(c.reason) <= 140 for c in db.cands.values())
            await link.stop()

    run(go())


def test_pause_resume_and_stop():
    async def go():
        async with StubRover() as r:
            r.hold_station.clear()
            link, db, fsm = await _setup(r)
            task = fsm.start_mission(MISSION, IdGen())
            await _wait(lambda: "station" in r.ops)
            fsm.pause()
            assert fsm.state == "PAUSED"
            r.hold_station.set()
            await asyncio.sleep(0.3)
            assert r.ops.count("station") == 1  # held between steps
            fsm.resume()
            assert fsm.state == "SURVEY"
            await _wait(lambda: r.ops.count("station") >= 2)
            r.hold_station.clear()
            await asyncio.sleep(0.05)
            await fsm.stop()
            assert fsm.state == "STOPPED" and task.done() and r.ops[-1] == "stop"
            await link.stop()

    run(go())


def test_estop_blocks_new_missions_until_cleared():
    async def go():
        async with StubRover() as r:
            r.hold_station.clear()
            link, db, fsm = await _setup(r)
            fsm.start_mission(MISSION, IdGen())
            await _wait(lambda: "station" in r.ops)
            await fsm.estop()
            assert fsm.state == "ESTOP"
            await _wait(lambda: r.estop)  # estop() returns once sent; the latch follows
            with pytest.raises(FsmBusy):
                fsm.start_mission(MISSION, IdGen())
            await fsm.clear_estop()
            assert fsm.state == "STOPPED" and not r.estop
            await link.stop()

    run(go())


def test_rover_error_and_vision_down_are_reported():
    async def go():
        async with StubRover() as r:
            r.fail["station"] = "E_CAMERA"
            link, db, fsm = await _setup(r)
            await fsm.start_mission(MISSION, IdGen())
            assert fsm.state == "STOPPED"
            assert any(lvl == "error" and "E_CAMERA" in m for _, lvl, m in db.events)
            await link.stop()

        class DownVision:
            async def score(self, run_id, items):
                raise ConnectionError("vision down")

        async with StubRover() as r:
            link, db, fsm = await _setup(r, DownVision())
            await fsm.start_mission({**MISSION, "radius_m": 0.5}, IdGen())
            assert fsm.state == "PLAN" and not db.cands
            assert any(k == "vision" and lvl == "error" for k, lvl, _ in db.events)
            await link.stop()

    run(go())
