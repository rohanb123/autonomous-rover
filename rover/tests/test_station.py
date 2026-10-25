"""rover/rover/station.py: the survey that feeds the GX10.

The bundle this produces is the only thing the mapper ingests, so these tests assert
against `StationJson`/`StationResult` themselves rather than against a dict shape -- if
the schema moves, these fail here instead of as a 422 from the real mapper.

The camera is `SyntheticSource`, the same frame `--camera sim` serves, so a green run here
and a laptop station run agree by construction.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[2] / "shared" / "py")]

import helpers  # noqa: E402

from marsrover_shared.ids import IdGen  # noqa: E402
from marsrover_shared.schemas import (  # noqa: E402
    MapperStationReply,
    Pose,
    StationJson,
    StationResult,
    StationUrls,
)
from rover.camera import Camera, SyntheticSource  # noqa: E402
from rover.config import parse_calib  # noqa: E402
from rover.motion import Aborted  # noqa: E402
from rover.station import Station, StationError  # noqa: E402

# A homography that is a real (if invented) measurement rather than the identity, plus the
# calibrated_at that tells station.py the geometry can be trusted.
CALIBRATED = helpers.calib_dict(
    calibrated_at="2026-10-24T12:00:00",
    bev={
        "bev_res_m_px": 0.005,
        "bev_size_px": 360,
        "homography": [[0.5, 0.0, 40.0], [0.0, 0.5, 60.0], [0.0, 0.0, 1.0]],
    },
)
UNCALIBRATED = helpers.calib_dict()  # calibrated_at: None, identity homography


class FakeMotion:
    """Records turns; the pose is advanced by the caller's PoseEstimator stand-in."""

    def __init__(self, pose: FakePose) -> None:
        self.pose = pose
        self.turns: list[tuple[float, float]] = []

    def turn(self, deg: float, speed: float) -> None:
        self.turns.append((deg, speed))
        self.pose.rotate(deg)


class FakePose:
    """A pose that only rotates, so the headings in the bundle are predictable."""

    def __init__(self, heading_deg: float = 0.0, x: float = 0.0, y: float = 0.0) -> None:
        self.pose = Pose(x=x, y=y, heading_deg=heading_deg, source="dr+gyro")

    def current(self) -> Pose:
        return self.pose

    def rotate(self, deg: float) -> None:
        from marsrover_shared.frames import wrap_deg

        self.pose = self.pose.model_copy(
            update={"heading_deg": wrap_deg(self.pose.heading_deg + deg)}
        )


class FakeSafety:
    """`cuts` is a list of codes to hand out, one per take_cut() call."""

    def __init__(self, cuts: list[str] | None = None, latched: bool = False) -> None:
        self.cuts = list(cuts or [])
        self._latched = latched

    def take_cut(self) -> str | None:
        return self.cuts.pop(0) if self.cuts else None

    def latched(self) -> bool:
        return self._latched

    def stop_reason(self) -> str | None:
        return "test cut"


class FakeStore:
    def __init__(self) -> None:
        self.saved: list[tuple[StationJson, dict[str, bytes]]] = []

    def save_station(self, station: StationJson, files: Any) -> str:
        self.saved.append((station, dict(files)))
        return f"runs/{station.run_id}/stations/{station.station_id}"


class FakeUploader:
    """Stands in for uploader.upload_station: one attempt, raises or replies.

    `urls.crops` comes back empty because populating it is the real mapper's job (it reads
    station.json server-side), and the uploader hands this fake only a path. The crop URLs
    are covered end to end against the actual mapper, not here.
    """

    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.calls: list[str] = []

    def upload_station(self, rel: str) -> MapperStationReply:
        self.calls.append(rel)
        if self.fail is not None:
            raise self.fail
        return MapperStationReply(
            ok=True,
            station_id=rel.rsplit("/", 1)[-1],
            stored=f"/data/{rel}/",
            urls=StationUrls(bev=f"http://gx10:8200/files/{rel}/bev.png", crops={}),
        )


class FakeServos:
    present = True

    def __init__(self) -> None:
        self.writes: list[tuple[int, int]] = []

    def write_us(self, ch: int, us: int) -> None:
        self.writes.append((ch, us))


def build(
    *,
    calib_src: dict | None = None,
    heading_deg: float = 0.0,
    safety: FakeSafety | None = None,
    store: FakeStore | None = None,
    uploader: FakeUploader | None = None,
    servos: Any = None,
    allow_uncalibrated: bool = False,
    run_id: str = "r_20261024_101500",
) -> tuple[Station, FakeStore, FakeMotion, list[tuple[str, dict]]]:
    calib = parse_calib(calib_src or CALIBRATED)
    camera = Camera(calib.camera, SyntheticSource(calib.camera.width, calib.camera.height))
    pose = FakePose(heading_deg=heading_deg)
    motion = FakeMotion(pose)
    st = store if store is not None else FakeStore()
    events: list[tuple[str, dict]] = []
    clock = helpers.FakeClock()
    station = Station(
        calib,
        camera,
        motion,
        pose,
        safety or FakeSafety(),
        store=st,
        uploader=uploader,
        servos=servos,
        ids=IdGen(),
        emit=lambda kind, detail: events.append((kind, detail)),
        clock=clock,
        sleep=clock.sleep,
        allow_uncalibrated=allow_uncalibrated,
    )
    station.run_id = run_id
    return station, st, motion, events


# --- the bundle ---------------------------------------------------------------------------


def test_result_is_a_valid_station_result():
    """The brain does exactly this (gx10/brain/fsm.py); anything less crashes its campaign."""
    station, _, _, _ = build(uploader=FakeUploader())
    result = station.run_station("s_0001", 8, 45.0)
    parsed = StationResult.model_validate(result)
    assert parsed.station_id == "s_0001"
    assert parsed.run_id == "r_20261024_101500"
    assert parsed.mapper.ok is True
    assert len(parsed.keyframes) == 8


def test_eight_views_eight_keyframes_seven_turns():
    station, store, motion, _ = build(uploader=FakeUploader())
    station.run_station("s_0001", 8, 45.0)
    bundle, files = store.saved[0]
    assert [kf.file for kf in bundle.keyframes] == [f"kf_{i:03d}.jpg" for i in range(8)]
    assert all(f"kf_{i:03d}.jpg" in files for i in range(8))
    # Seven, not eight: there is no point turning after the last photograph.
    assert len(motion.turns) == 7
    assert [deg for deg, _ in motion.turns] == [45.0] * 7


def test_keyframe_ids_are_sequential_and_run_scoped():
    station, store, _, _ = build(uploader=FakeUploader())
    station.run_station("s_0001", 4, 90.0)
    assert [kf.kf_id for kf in store.saved[0][0].keyframes] == [
        "k_0001",
        "k_0002",
        "k_0003",
        "k_0004",
    ]


def test_a_second_station_continues_the_keyframe_ids():
    """Ids are unique within a RUN, not within a station (AGENTS.md section 4)."""
    station, store, _, _ = build(uploader=FakeUploader())
    station.run_station("s_0001", 2, 180.0)
    station.run_station("s_0002", 2, 180.0)
    assert [kf.kf_id for kf in store.saved[1][0].keyframes] == ["k_0003", "k_0004"]


# --- the heading rule (contract 1.3.0) ----------------------------------------------------


def test_keyframe_headings_are_absolute_not_relative():
    """A station starting at 30 deg records 30, 75, 120 ... not 0, 45, 90.

    doc 00 section 6.4: the mapper copies heading_deg straight into the DB, so a relative
    value misplaces every keyframe after the first and nothing errors.
    """
    station, store, _, _ = build(heading_deg=30.0, uploader=FakeUploader())
    station.run_station("s_0001", 4, 45.0)
    headings = [kf.heading_deg for kf in store.saved[0][0].keyframes]
    assert headings == pytest.approx([30.0, 75.0, 120.0, 165.0])


def test_keyframe_headings_wrap_to_the_contract_range():
    """(-180, 180], not 0..360 (doc 00 section 4)."""
    station, store, _, _ = build(heading_deg=170.0, uploader=FakeUploader())
    station.run_station("s_0001", 3, 45.0)
    headings = [kf.heading_deg for kf in store.saved[0][0].keyframes]
    assert headings == pytest.approx([170.0, -145.0, -100.0])
    assert all(-180 < h <= 180 for h in headings)


def test_the_bundle_pose_is_where_the_station_started():
    station, store, _, _ = build(heading_deg=90.0, uploader=FakeUploader())
    station.run_station("s_0001", 8, 45.0)
    assert store.saved[0][0].pose.heading_deg == pytest.approx(90.0)


# --- proposals and the mosaic -------------------------------------------------------------


def test_proposals_and_crops_are_produced_when_calibrated():
    station, store, _, _ = build(uploader=FakeUploader())
    station.run_station("s_0001", 8, 45.0)
    bundle, files = store.saved[0]
    assert bundle.proposals, "the synthetic frame has three rocks in it"
    for p in bundle.proposals:
        assert p.crop in files, f"{p.prop_id} declares a crop that is not in the bundle"
        assert files[p.crop].startswith(b"\xff\xd8"), "crops must be real JPEGs"
        assert p.kf_id.startswith("k_")
        assert len(p.bbox_px) == 4
        assert p.size_m > 0


def test_proposal_ids_are_dense_after_dedupe():
    """A p_0007 deduped away would make the dashboard look like it lost a candidate."""
    station, store, _, _ = build(uploader=FakeUploader())
    station.run_station("s_0001", 8, 45.0)
    ids = [p.prop_id for p in store.saved[0][0].proposals]
    assert ids == [f"p_{i:04d}" for i in range(1, len(ids) + 1)]


def test_dedupe_collapses_the_same_rock_seen_twice():
    """The synthetic frame is identical from every view, so with no rotation applied to the
    pose every view finds the same three rocks at the same mission xy. They must collapse to
    three proposals, not twenty-four."""
    station, store, motion, _ = build(uploader=FakeUploader())
    motion.turn = lambda deg, speed: motion.turns.append((deg, speed))  # pose never moves
    station.run_station("s_0001", 8, 45.0)
    assert len(store.saved[0][0].proposals) == 3


def test_bev_png_is_in_the_bundle_and_is_a_png():
    station, store, _, _ = build(uploader=FakeUploader())
    station.run_station("s_0001", 8, 45.0)
    bundle, files = store.saved[0]
    assert bundle.bev.file == "bev.png"
    assert files["bev.png"].startswith(b"\x89PNG")
    assert bundle.bev.frame == "rover@station"
    assert bundle.bev.res_m_px == pytest.approx(0.005)
    assert bundle.bev.w_px == bundle.bev.h_px == 360


# --- the uncalibrated path ----------------------------------------------------------------


def test_refuses_to_run_uncalibrated_by_default():
    station, _, _, _ = build(calib_src=UNCALIBRATED, uploader=FakeUploader())
    with pytest.raises(StationError, match="calibrated_at"):
        station.run_station("s_0001", 8, 45.0)


def test_uncalibrated_uploads_keyframes_only_and_claims_no_geometry():
    """Real photos and real headings; no bev.png part and no proposals, so nothing the
    mapper stores is geometrically invented. The mapper warns and still answers ok."""
    station, store, _, _ = build(
        calib_src=UNCALIBRATED, uploader=FakeUploader(), allow_uncalibrated=True
    )
    result = station.run_station("s_0001", 8, 45.0)
    bundle, files = store.saved[0]
    assert len(bundle.keyframes) == 8
    assert bundle.proposals == []
    assert "bev.png" not in files
    # bev metadata is still declared: the schema requires it, and the mapper answers a
    # missing FILE with a warning rather than a 422.
    assert bundle.bev.file == "bev.png"
    assert StationResult.model_validate(result).mapper.ok is True


def test_refuses_without_a_local_store():
    station, _, _, _ = build()
    station.store = None
    with pytest.raises(StationError, match="--store"):
        station.run_station("s_0001", 8, 45.0)


def test_refuses_without_a_camera():
    station, _, _, _ = build()
    station.camera = type("NoCam", (), {"present": False})()
    with pytest.raises(StationError, match="no camera"):
        station.run_station("s_0001", 8, 45.0)


def test_rejects_a_nonsense_view_count():
    station, _, _, _ = build()
    with pytest.raises(StationError, match="n_views"):
        station.run_station("s_0001", 0, 45.0)


# --- upload behaviour ---------------------------------------------------------------------


def test_a_failed_upload_still_answers_done():
    """Role doc section 5.6: the photos are safe on the SD card, so this is not a failure.
    The retry thread has the bundle."""
    uploader = FakeUploader(fail=RuntimeError("mapper unreachable"))
    station, store, _, events = build(uploader=uploader)
    result = station.run_station("s_0001", 8, 45.0)
    parsed = StationResult.model_validate(result)
    assert parsed.mapper.ok is False
    assert parsed.mapper.station_id == "s_0001"
    assert parsed.mapper.urls.bev == "", "no URL may be invented for a bundle not uploaded"
    assert parsed.mapper.urls.crops == {}
    assert store.saved, "it must still be on disk"
    assert any(kind == "station_upload_deferred" for kind, _ in events)


def test_the_bundle_is_stored_before_it_is_uploaded():
    """If the upload is tried first, a crash loses the photographs."""
    order: list[str] = []
    store = FakeStore()
    real_save = store.save_station

    def tracked(station: StationJson, files: Any) -> str:
        order.append("save")
        return real_save(station, files)

    store.save_station = tracked  # type: ignore[method-assign]
    uploader = FakeUploader()
    real_upload = uploader.upload_station

    def tracked_upload(rel: str) -> MapperStationReply:
        order.append("upload")
        return real_upload(rel)

    uploader.upload_station = tracked_upload  # type: ignore[method-assign]
    station, _, _, _ = build(store=store, uploader=uploader)
    station.run_station("s_0001", 2, 180.0)
    assert order == ["save", "upload"]


def test_no_uploader_configured_is_honest_about_it():
    station, store, _, _ = build(uploader=None)
    result = station.run_station("s_0001", 2, 180.0)
    parsed = StationResult.model_validate(result)
    assert parsed.mapper.ok is False
    assert "s_0001" in parsed.mapper.stored
    assert store.saved


# --- safety -------------------------------------------------------------------------------


def test_an_estop_between_views_aborts_the_station():
    safety = FakeSafety(cuts=[None, None, "E_ESTOP"])  # type: ignore[list-item]
    station, store, _, _ = build(safety=safety, uploader=FakeUploader())
    with pytest.raises(Aborted) as e:
        station.run_station("s_0001", 8, 45.0)
    assert e.value.code == "E_ESTOP"
    assert not store.saved, "a partial station must not be uploaded"


def test_a_tilt_cut_aborts_with_its_own_code():
    safety = FakeSafety(cuts=[None, "E_TILT"])  # type: ignore[list-item]
    station, _, _, _ = build(safety=safety, uploader=FakeUploader())
    with pytest.raises(Aborted) as e:
        station.run_station("s_0001", 8, 45.0)
    assert e.value.code == "E_TILT"


def test_a_latched_safety_refuses_before_the_first_photo():
    station, store, motion, _ = build(safety=FakeSafety(latched=True), uploader=FakeUploader())
    with pytest.raises(Aborted) as e:
        station.run_station("s_0001", 8, 45.0)
    assert e.value.code == "E_ESTOP"
    assert motion.turns == [], "the rover must not move while latched"


# --- the scoop and the camera -------------------------------------------------------------


def test_the_scoop_is_stowed_before_the_first_photo():
    """Otherwise it is in all eight of them (role doc section 5 step 1)."""
    servos = FakeServos()
    station, _, _, _ = build(servos=servos, uploader=FakeUploader())
    station.run_station("s_0001", 2, 180.0)
    stow = station.calib.scoop_us["STOW"]
    assert servos.writes[:2] == [
        (station.calib.servos.shoulder_ch, stow[0]),
        (station.calib.servos.wrist_ch, stow[1]),
    ]


def test_a_missing_scoop_does_not_cost_the_survey():
    class Broken(FakeServos):
        def write_us(self, ch: int, us: int) -> None:
            raise RuntimeError("no servo on that channel")

    station, store, _, _ = build(servos=Broken(), uploader=FakeUploader())
    station.run_station("s_0001", 2, 180.0)
    assert len(store.saved[0][0].keyframes) == 2


def test_the_exposure_lock_is_reported_not_assumed():
    """docs/05 U-21: camera.py verifies the lock from frame metadata, so the station should
    log what actually happened."""
    station, _, _, events = build(uploader=FakeUploader())
    station.run_station("s_0001", 2, 180.0)
    locked = [d for kind, d in events if kind == "camera_locked"]
    assert locked and locked[0]["exposure_locked"] is True


def test_events_bracket_the_station():
    station, _, _, events = build(uploader=FakeUploader())
    station.run_station("s_0001", 8, 45.0)
    kinds = [k for k, _ in events]
    assert kinds[0] == "station_start"
    assert kinds[-1] == "station_done"
    done = events[-1][1]
    assert done["keyframes"] == 8
    assert done["uploaded"] is True
    assert done["bev"] is True
