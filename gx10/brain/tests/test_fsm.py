import asyncio
import math

import pytest

from brain.fsm import FsmBusy, NotNow, RunFSM
from brain.rover_link import RoverLink
from brain.tests.stub_rover import ROCKS, StubMapper, StubRover, StubVision
from marsrover_shared.ids import IdGen

MISSION = {"mission_id": "m_0001", "run_id": "r_test", "text": "t", "radius_m": 1.5, "k": 3,
           "categories": ["float_rock"], "sample": True, "return_home": True,
           "max_duration_s": 900, "mode": "act", "assumptions": []}  # fmt: skip
PRIORS = {"float_rock": 1.0, "pebble": 0.5}


class FakeDb:
    def __init__(self):
        self.events, self.tasks, self.cands, self.missions = [], {}, {}, []
        self.visits, self.analyses, self.ended = [], [], []

    def event(self, kind, msg, level="info", data=None):
        self.events.append((kind, level, msg))

    def mission(self, m, status):
        self.missions.append(status)

    def task(self, tid, run, mission, kind, ref, seq, status, note=""):
        self.tasks[tid] = (kind, ref, seq, status)

    def candidate(self, c, comps):
        self.cands[c.cand_id] = c

    def visit(self, v):
        self.visits.append(v)

    def analysis(self, run_id, a, sample_id, cand_id, visit_id, image_url):
        self.analyses.append((cand_id, a, image_url))

    def pose(self, run_id, pose, t):
        self.last_pose = pose

    def run_ended(self, run_id, status="done"):
        self.ended.append((run_id, status))


class Rig:
    """Stub rover + link + FSM, torn down with the event loop."""

    def __init__(self, r, vision=None, mapper=None, overhead=None):
        self.r, self.vision, self.mapper = r, vision or StubVision(), mapper or StubMapper()
        self.overhead = overhead

    async def __aenter__(self):
        self.link = RoverLink(self.r.url)
        self.link.start()
        for _ in range(150):
            if self.link.connected and self.link.last_telemetry:
                break
            await asyncio.sleep(0.02)
        self.db = FakeDb()
        self.fsm = RunFSM(self.link, self.vision, self.db, PRIORS, self.mapper, self.overhead)
        self.run_ended = []
        self.fsm.on_run_ended = self.run_ended.append
        return self

    async def __aexit__(self, *exc):
        await self.link.stop()


async def _wait(cond, timeout=8.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 30))


def outcomes(db):
    return [v.outcome for v in db.visits]


def test_full_campaign_survey_to_done():
    async def go():
        async with StubRover() as r, Rig(r) as g:
            await g.fsm.start_mission(MISSION, IdGen())
            db = g.db
            assert g.fsm.state == "DONE"
            assert r.ops.count("station") == 5
            assert len(db.cands) == 5 and any(len(c.prop_ids) > 1 for c in db.cands.values())
            assert outcomes(db) == ["sampled"] * 3 and r.ops.count("sample") == 3
            assert all(a.assay.seed == 3896889563 for _, a, _ in db.analyses)
            visited_ids = {v.cand_id for v in db.visits}
            fsm_cands = g.fsm.cands  # the mark_visited reducer updates the real DB row
            assert all(
                fsm_cands[c].status == "visited" and fsm_cands[c].rank == -1 for c in visited_ids
            )
            # back home, facing +x; run ended and fused
            assert math.hypot(r.x, r.y) < 0.01 and abs(r.heading) < 2.5
            assert abs(db.last_pose.heading_deg) < 2.5  # final pose recorded at home
            assert db.missions == ["active", "done"] and db.ended == [("r_test", "done")]
            assert g.mapper.fused == ["r_test"] and g.run_ended == ["r_test"]
            kinds = [t[0] for t in db.tasks.values()]
            assert kinds.count("station") == 5 and kinds.count("visit") == 3
            assert all(t[3] == "done" for t in db.tasks.values())

    run(go())


def test_an_unusable_closeup_retries_once_then_records_skipped():
    """Contract 2.0.0 has no empty scoop, but it has an unusable close-up: vision
    returns assay=None when the target is not in frame, and that still earns one retry."""

    async def go():
        async with StubRover() as r, Rig(r) as g:
            g.vision.empty_first = 2  # the first target is not in frame twice
            await g.fsm.start_mission(MISSION, IdGen())
            first = g.vision.analyzed[0]
            skipped = [v for v in g.db.visits if v.outcome == "skipped"]
            assert [v.cand_id for v in skipped] == [first]
            assert g.vision.analyzed.count(first) == 2  # one retry
            _, analysis, _ = next(a for a in g.db.analyses if a[0] == first)
            assert analysis is not None and analysis.assay is None
            assert len(g.db.visits) == 3  # an unusable close-up still counts toward k

    run(go())


def test_lost_target_is_unreachable_and_counts():
    async def go():
        async with StubRover() as r, Rig(r) as g:
            r.lose_first = True
            await g.fsm.start_mission(MISSION, IdGen())
            assert outcomes(g.db) == ["unreachable", "sampled", "sampled"]
            assert g.fsm.cands[g.db.visits[0].cand_id].status == "failed"

    run(go())


def test_skip_is_replaced_by_the_next_best():
    async def go():
        async with StubRover() as r, Rig(r) as g:
            r.hold_sample.clear()
            task = g.fsm.start_mission(MISSION, IdGen())
            await _wait(lambda: "sample" in r.ops)
            first = g.fsm.active_cand
            g.fsm.skip()
            r.hold_sample.set()
            await task
            assert g.db.visits[0].cand_id == first and g.db.visits[0].outcome == "skipped"
            assert outcomes(g.db).count("sampled") == 3  # the skip was replaced
            assert g.fsm.state == "DONE"
            with pytest.raises(NotNow):
                g.fsm.skip()

    run(go())


def test_come_back_mid_visit_requeues_and_returns_home():
    async def go():
        async with StubRover() as r, Rig(r) as g:
            r.hold_sample.clear()
            task = g.fsm.start_mission(MISSION, IdGen())
            await _wait(lambda: "sample" in r.ops)
            target = g.fsm.active_cand
            g.fsm.come_back()
            r.hold_sample.set()
            await task
            assert g.fsm.state == "DONE" and math.hypot(r.x, r.y) < 0.01
            assert g.db.cands[target].status == "queued" and not g.db.visits

    run(go())


def test_suggest_mode_waits_for_approval():
    async def go():
        async with StubRover() as r, Rig(r) as g:
            task = g.fsm.start_mission({**MISSION, "mode": "suggest"}, IdGen())
            await _wait(lambda: g.fsm.state == "AWAIT_APPROVAL")
            assert "sample" not in r.ops
            with pytest.raises(NotNow):
                g.fsm.approve("m_0009", True)
            g.fsm.approve("m_0001", True)
            await task
            assert outcomes(g.db) == ["sampled"] * 3

        async with StubRover() as r, Rig(r) as g:
            task = g.fsm.start_mission({**MISSION, "mode": "suggest"}, IdGen())
            await _wait(lambda: g.fsm.state == "AWAIT_APPROVAL")
            g.fsm.approve("m_0001", False)
            await task
            assert g.fsm.state == "STOPPED" and g.db.missions[-1] == "rejected"

    run(go())


def test_look_only_and_time_up():
    async def go():
        async with StubRover() as r, Rig(r) as g:
            await g.fsm.start_mission({**MISSION, "sample": False}, IdGen())
            assert "sample" not in r.ops and r.ops.count("snapshot") == 3
            assert outcomes(g.db) == ["skipped"] * 3 and not g.db.analyses
            assert g.fsm.state == "DONE"

        async with StubRover() as r, Rig(r) as g:
            # Windows' monotonic clock ticks every 15.6 ms and asyncio runs any timer due
            # within one tick at once, so the stub's default 0.01 s ops take no time and
            # the whole survey can fit in one tick. 0.03 s ops make the budget run out.
            r.op_delay_s = 0.03
            await g.fsm.start_mission({**MISSION, "max_duration_s": 0.01}, IdGen())
            assert not g.db.visits and g.fsm.state == "DONE"
            assert any("time up" in m for _, _, m in g.db.events)

    run(go())


def test_pause_resume_stop_and_come_back_after_stop():
    async def go():
        async with StubRover() as r, Rig(r) as g:
            r.hold_station.clear()
            task = g.fsm.start_mission(MISSION, IdGen())
            await _wait(lambda: "station" in r.ops)
            g.fsm.pause()
            assert g.fsm.state == "PAUSED"
            r.hold_station.set()
            await asyncio.sleep(0.3)
            assert r.ops.count("station") == 1  # held between steps
            g.fsm.resume()
            await _wait(lambda: r.ops.count("station") >= 2)
            r.hold_station.clear()
            await asyncio.sleep(0.05)
            await g.fsm.stop()
            assert g.fsm.state == "STOPPED" and task.done() and r.ops[-1] == "stop"
            r.hold_station.set()
            await g.fsm.come_back()
            assert g.fsm.state == "DONE" and math.hypot(r.x, r.y) < 0.01

    run(go())


def test_estop_blocks_new_missions_until_cleared():
    async def go():
        async with StubRover() as r, Rig(r) as g:
            r.hold_station.clear()
            g.fsm.start_mission(MISSION, IdGen())
            await _wait(lambda: "station" in r.ops)
            await g.fsm.estop()
            assert g.fsm.state == "ESTOP"
            await _wait(lambda: r.estop)  # estop() returns once sent; the latch follows
            with pytest.raises(FsmBusy):
                g.fsm.start_mission(MISSION, IdGen())
            await g.fsm.clear_estop()
            assert g.fsm.state == "STOPPED" and not r.estop

    run(go())


def test_rover_error_and_vision_down_are_reported():
    async def go():
        async with StubRover() as r, Rig(r) as g:
            r.fail["station"] = "E_CAMERA"
            await g.fsm.start_mission(MISSION, IdGen())
            # A failed station is reported and skipped: the rover still drives the survey
            # pattern and comes home instead of stopping where the error found it.
            assert g.fsm.state == "DONE" and r.ops.count("station") == 5
            assert any(lvl == "error" and "E_CAMERA" in m for _, lvl, m in g.db.events)
            statuses = [t[3] for t in g.db.tasks.values() if t[0] == "station"]
            assert statuses == ["failed"] * 5
            assert math.hypot(r.x, r.y) < 0.01 and g.run_ended == ["r_test"]

        class DownVision(StubVision):
            async def score(self, run_id, items):
                raise ConnectionError("vision down")

        async with StubRover() as r, Rig(r, DownVision()) as g:
            await g.fsm.start_mission({**MISSION, "radius_m": 0.5}, IdGen())
            assert g.fsm.state == "DONE" and not g.db.cands and not g.db.visits
            assert any(k == "vision" and lvl == "error" for k, lvl, _ in g.db.events)

    run(go())


def test_a_config_fault_on_a_target_sends_the_rover_home():
    async def go():
        async with StubRover() as r, Rig(r) as g:
            r.fail["approach"] = "E_INTERNAL"
            await g.fsm.start_mission(MISSION, IdGen())
            assert g.fsm.state == "DONE" and r.ops.count("approach") == 1
            assert not g.db.visits  # not a rock out of reach: nothing is recorded against it
            assert math.hypot(r.x, r.y) < 0.01

    run(go())


def test_a_failed_approach_is_unreachable_and_the_run_carries_on():
    async def go():
        async with StubRover() as r, Rig(r) as g:
            r.fail["approach"] = "E_TIMEOUT"
            await g.fsm.start_mission(MISSION, IdGen())
            assert g.fsm.state == "DONE"
            assert outcomes(g.db) == ["unreachable"] * 3  # every target tried, none sampled
            assert r.ops.count("approach") == 3 and "sample" not in r.ops
            assert math.hypot(r.x, r.y) < 0.01  # came home

    run(go())


def test_a_safety_error_still_ends_the_campaign():
    async def go():
        async with StubRover() as r, Rig(r) as g:
            r.fail["station"] = "E_TILT"
            await g.fsm.start_mission(MISSION, IdGen())
            assert g.fsm.state == "STOPPED" and r.ops.count("station") == 1

    run(go())


def test_rocks_fixture_sanity():
    assert len(ROCKS) == 5


class FixedOverhead:
    """Reports the rover 4 cm off its dead-reckoned spot, as if drift had built up."""

    def __init__(self, rover, reproj=0.7):
        self.rover, self.reproj = rover, reproj

    def usable(self):
        from marsrover_shared.schemas import OverheadPose

        if self.reproj >= 2.0:  # the real link filters these out
            return None
        return OverheadPose(
            x=self.rover.x + 0.04,
            y=self.rover.y,
            heading_deg=self.rover.heading,
            marker_id=0,
            reproj_err_px=self.reproj,
            visible=True,
        )


def test_aruco_resets_pose_before_each_station():
    async def go():
        async with StubRover() as r:
            async with Rig(r, overhead=FixedOverhead(r)) as g:
                await g.fsm.start_mission({**MISSION, "k": 1}, IdGen())
                assert len(r.pose_resets) == 5 and all(s[3] == "aruco" for s in r.pose_resets)
                stations = [i for i, op in enumerate(r.ops) if op == "station"]
                assert all(r.ops[i - 1] == "set_pose" for i in stations)
                notes = [m for k, _, m in g.db.events if k == "pose"]
                assert len(notes) == 5 and "ArUco correction 4.0 cm" in notes[0]
                assert g.fsm.state == "DONE"
        async with StubRover() as r:
            async with Rig(r, overhead=FixedOverhead(r, reproj=3.0)) as g:
                await g.fsm.start_mission({**MISSION, "k": 1}, IdGen())
                assert not r.pose_resets and "set_pose" not in r.ops

    run(go())
