import math
from datetime import datetime

import pytest

from marsrover_shared.frames import mission_to_rover, rover_to_mission, wrap_deg
from marsrover_shared.ids import IdGen, new_run_id
from marsrover_shared.schemas import Pose


def test_run_id_format():
    assert new_run_id(datetime(2026, 10, 24, 10, 15, 0)) == "r_20261024_101500"


def test_idgen_pads_and_counts_per_prefix():
    g = IdGen()
    assert [g.next("c"), g.next("c"), g.next("s")] == ["c_0001", "c_0002", "s_0001"]
    assert g.next("q") == "q_000001"
    g.observe("c_0041")
    assert g.next("c") == "c_0042"
    with pytest.raises(ValueError):
        g.next("z")


@pytest.mark.parametrize(
    "a, want",
    [
        (0, 0),
        (180, 180),
        (-180, 180),
        (190, -170),
        (-190, 170),
        (540, 180),
        (-540, 180),
        (720, 0),
        (359, -1),
    ],
)
def test_wrap_deg_range(a, want):
    assert wrap_deg(a) == pytest.approx(want)


def test_frame_transforms_invert():
    pose = Pose(x=1.0, y=2.0, heading_deg=90.0, source="dr")
    # Rover facing +y in the mission frame: 1 m ahead is (1, 3); 1 m left is (0, 2).
    assert rover_to_mission(pose, (1.0, 0.0)) == pytest.approx((1.0, 3.0))
    assert rover_to_mission(pose, (0.0, 1.0)) == pytest.approx((0.0, 2.0))
    for h in (-135.0, -30.0, 0.0, 45.0, 179.0):
        p = Pose(x=-0.4, y=0.7, heading_deg=h, source="dr")
        xy = (0.31, -1.2)
        assert mission_to_rover(p, rover_to_mission(p, xy)) == pytest.approx(xy)
    assert math.isclose(wrap_deg(pose.heading_deg + 360), 90.0)
