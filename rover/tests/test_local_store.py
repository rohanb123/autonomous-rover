"""LocalStore: bundle layout, atomic writes, and a queue that survives a restart."""

from __future__ import annotations

import json

import pytest

from marsrover_shared.schemas import Bev, Pose, SampleJson, StationJson
from rover.local_store import LocalStore, StoreError

RUN = "r_20261024_101500"
POSE = Pose(x=0.8, y=0.0, heading_deg=0.0, source="dr+gyro")


def station(sid: str = "s_0001") -> StationJson:
    return StationJson(
        run_id=RUN,
        station_id=sid,
        t=1761300100.0,
        pose=POSE,
        keyframes=[],
        bev=Bev(file="bev.png", res_m_px=0.005, w_px=360, h_px=360, frame="rover@station"),
        proposals=[],
    )


def sample(aid: str = "a_0001") -> SampleJson:
    return SampleJson(run_id=RUN, sample_id=aid, cand_id="c_0001", t=1761300200.0, pose=POSE)


FILES = {"kf_000.jpg": b"\xff\xd8kf0", "bev.png": b"\x89PNGbev"}


def test_station_bundle_layout(tmp_path):
    store = LocalStore(tmp_path)
    rel = store.save_station(station(), FILES)
    assert rel == f"{RUN}/stations/s_0001"
    folder = tmp_path / rel
    assert sorted(p.name for p in folder.iterdir()) == ["bev.png", "kf_000.jpg", "station.json"]
    assert json.loads((folder / "station.json").read_text())["station_id"] == "s_0001"
    assert not list((tmp_path / RUN / "stations").glob(".tmp-*"))


def test_load_round_trips(tmp_path):
    store = LocalStore(tmp_path)
    rel = store.save_sample(sample(), b"\xff\xd8present")
    bundle = store.load(rel)
    assert bundle.kind == "sample" and bundle.meta_name == "sample.json"
    assert dict(bundle.files) == {"closeup.jpg": b"\xff\xd8present"}
    assert SampleJson.model_validate_json(bundle.meta_json) == sample()


def test_pending_is_oldest_first_and_marking_removes(tmp_path):
    store = LocalStore(tmp_path)
    a = store.save_station(station("s_0001"), FILES)
    b = store.save_station(station("s_0002"), FILES)
    assert store.pending_uploads() == [a, b] and store.backlog == 2
    store.mark_failed(a, "timeout")
    assert store.status(a) == (0, 1, "timeout") and store.backlog == 2
    store.mark_uploaded(a)
    store.mark_rejected(b, "422")
    assert store.pending_uploads() == [] and store.backlog == 0
    assert store.status(b)[0] == -1


def test_queue_survives_a_restart(tmp_path):
    first = LocalStore(tmp_path)
    rel = first.save_station(station(), FILES)
    first.close()
    again = LocalStore(tmp_path)  # the Pi rebooted
    assert again.pending_uploads() == [rel]
    assert dict(again.load(rel).files) == FILES


def test_failed_write_leaves_nothing_behind(tmp_path):
    store = LocalStore(tmp_path)
    with pytest.raises(TypeError):
        store.save_station(station(), {"kf_000.jpg": b"ok", "bev.png": object()})  # type: ignore[dict-item]
    assert store.pending_uploads() == []
    assert not (tmp_path / RUN / "stations" / "s_0001").exists()
    assert not list((tmp_path / RUN / "stations").glob(".tmp-*"))


@pytest.mark.parametrize("name", ["../escape.jpg", "sub/kf.jpg", "station.json"])
def test_rejects_unsafe_file_names(tmp_path, name):
    with pytest.raises(ValueError):
        LocalStore(tmp_path).save_station(station(), {name: b"x"})


def test_missing_bundle_is_a_store_error(tmp_path):
    with pytest.raises(StoreError):
        LocalStore(tmp_path).load("r_x/stations/s_9999")
