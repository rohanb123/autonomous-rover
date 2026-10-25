"""A station survey: spin in place, photograph the ground, propose rocks, upload the bundle.

This is the rover's core deliverable (P2 role doc section 5) and the only thing that puts
data into the GX10: `POST /ingest/station` is what produces `keyframe` and `local_map_tile`
rows, and the mapper's crop URLs are what gx10/vision's /score scores.

Everything here is glue. The pieces already exist:
    camera.py    grab(), lock_exposure(), focus_for_survey(), jpeg()
    motion.py    turn()        -- gyro-closed when the MPU-6050 answers
    pose.py      current()     -- the absolute mission-frame pose
    bev.py       warp_to_bev(), pixel_to_rover_xy(), stitch_station()
    proposals.py find_rocks()
    frames.py    rover_to_mission()
    local_store.py / uploader.py   atomic save, then one upload attempt

Two rules that are easy to break and silent when broken:

1. `keyframes[].heading_deg` is the ABSOLUTE mission-frame heading at capture (doc 00
   section 6.4, contract 1.3.0). It is read from the live pose, never computed as
   `start + i*step`: the mapper copies it straight into the DB, so a relative value
   misplaces every keyframe after the first and nothing errors.
2. The op answers `done` even when the upload fails (role doc section 5.6). A station whose
   photos are safe on the SD card is not a failed station; the retry thread has the bundle.

Deliberately NOT here: role doc section 5 step 3 fuses visual-odometry yaw between
consecutive BEVs via `heading.py::vo_yaw_deg`, which is unwritten (I4). Headings come from
the gyro-closed pose alone. The seam is `_capture_views`, which already holds both BEVs.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

import numpy as np

from marsrover_shared.frames import rover_to_mission
from marsrover_shared.ids import IdGen
from marsrover_shared.schemas import (
    Bev,
    MapperStationReply,
    Pose,
    Proposal,
    StationJson,
    StationKeyframe,
    StationUrls,
)
from rover import bev as bev_mod
from rover import proposals as prop_mod
from rover.config import Calib
from rover.motion import Aborted

log = logging.getLogger("rover.station")

#: Crop size around a proposal's bbox, px. 96 is what the role doc section 5 specifies.
CROP_PX = 96

#: Proposals this close together within one station are the same rock seen from two views
#: (role doc section 5 step 5). Mission-frame metres.
DEDUPE_M = 0.05

#: Hard ceiling on proposals in one bundle. Eight views of a gravelly arena can find a lot,
#: and every one costs a crop in the upload and a /score call on the GX10. They are sorted
#: by contrast, so this keeps the best.
MAX_PROPOSALS = 24


class StationError(RuntimeError):
    """A station that cannot start. The caller turns it into an `err`."""


class _Store(Protocol):
    def save_station(self, station: StationJson, files: Any) -> str: ...


class _Uploader(Protocol):
    def upload_station(self, rel: str) -> MapperStationReply: ...


class _Raw:
    """One proposal before it has an id: pixels, mission xy and the view it came from."""

    __slots__ = ("bbox_px", "contrast", "crop", "kf_id", "size_m", "xy")

    def __init__(
        self,
        kf_id: str,
        bbox_px: list[int],
        xy: tuple[float, float],
        size_m: float,
        contrast: float,
        crop: bytes,
    ) -> None:
        self.kf_id = kf_id
        self.bbox_px = bbox_px
        self.xy = xy
        self.size_m = size_m
        self.contrast = contrast
        self.crop = crop


class Station:
    """Runs one station survey. Built once per run; `run_station` is called per station.

    Blocking throughout -- it runs on the op worker thread, and `HardwareBackend._offload`
    is what keeps it off the event loop.
    """

    def __init__(
        self,
        calib: Calib,
        camera: Any,
        motion: Any,
        pose: Any,
        safety: Any,
        *,
        store: _Store | None = None,
        uploader: _Uploader | None = None,
        servos: Any = None,
        ids: IdGen | None = None,
        emit: Any = None,
        clock: Any = time.time,
        sleep: Any = time.sleep,
        allow_uncalibrated: bool = False,
    ) -> None:
        self.calib = calib
        self.camera = camera
        self.motion = motion
        self.pose = pose
        self.safety = safety
        self.store = store
        self.uploader = uploader
        self.servos = servos
        self.ids = ids or IdGen()
        self.emit = emit or (lambda kind, detail: None)
        self.clock = clock
        self.sleep = sleep
        self.allow_uncalibrated = allow_uncalibrated
        self.run_id = ""

    # --- gating ---------------------------------------------------------------------------

    @property
    def geometry_ok(self) -> bool:
        """Is the homography a measurement, or calib.yaml's identity placeholder?

        When it is the placeholder, a proposal's `xy` and the BEV mosaic are geometrically
        meaningless -- so the station uploads keyframes only, rather than nonsense the
        mapper and the brain cannot tell from a real survey.
        """
        return self.calib.calibrated

    def preflight(self) -> None:
        """Raise if a station cannot run at all. Checked before the rover moves."""
        if not self.camera.present:
            raise StationError("no camera: a station is 8 photographs, so there is nothing to do")
        if self.store is None:
            raise StationError(
                "no local store: start the runtime with --store, or the bundle has nowhere "
                "to go and the photos would be lost"
            )
        if not self.geometry_ok and not self.allow_uncalibrated:
            raise StationError(
                "calib.yaml has calibrated_at: null, so the homography is the identity "
                "placeholder and every proposal xy would be fiction. Run "
                "rover/calib/calibrate_camera.py, or pass --allow-uncalibrated to upload "
                "keyframes only"
            )

    # --- the survey -----------------------------------------------------------------------

    def run_station(self, station_id: str, n_views: int, step_deg: float) -> dict[str, Any]:
        """Capture, assemble, store and upload one station. Returns a `StationResult` dict."""
        if n_views <= 0:
            raise StationError(f"n_views must be positive, got {n_views}")
        self.preflight()
        t0 = self.clock()
        start_pose = self.pose.current()
        self._event(
            "station_start",
            station_id,
            {"n_views": n_views, "step_deg": step_deg, "calibrated": self.geometry_ok},
        )

        self._stow_scoop()
        self._prepare_camera()
        keyframes, jpegs, views, raw = self._capture_views(n_views, step_deg)

        files: dict[str, bytes] = dict(jpegs)
        proposals: list[Proposal] = []
        if self.geometry_ok:
            files["bev.png"] = self._mosaic(views)
            proposals = self._finalize_proposals(raw, files)

        station = StationJson(
            run_id=self.run_id,
            station_id=station_id,
            t=t0,
            pose=start_pose,
            keyframes=keyframes,
            # Declared even when no bev.png part is sent: `bev` is required by the schema,
            # and the mapper answers a missing file with a warning and ok=True. That is what
            # makes the uncalibrated path legal rather than a 422.
            bev=Bev(
                file="bev.png",
                res_m_px=self.calib.bev_res_m_px,
                w_px=self.calib.bev_size_px,
                h_px=self.calib.bev_size_px,
                frame="rover@station",
            ),
            proposals=proposals,
        )
        reply = self._store_and_upload(station, files)
        self._event(
            "station_done",
            station_id,
            {
                "keyframes": len(keyframes),
                "proposals": len(proposals),
                "bev": "bev.png" in files,
                "uploaded": reply.ok,
                "secs": round(self.clock() - t0, 2),
            },
        )
        # StationResult = station.json + the mapper's reply. The brain validates the whole
        # thing (gx10/brain/fsm.py), so a missing `mapper` key crashes its campaign.
        return {**station.model_dump(mode="json"), "mapper": reply.model_dump(mode="json")}

    # --- steps ----------------------------------------------------------------------------

    def _stow_scoop(self) -> None:
        """STOW before the first photo, or the scoop is in all eight of them."""
        if self.servos is None or not getattr(self.servos, "present", False):
            return
        stow = self.calib.scoop_us.get("STOW")
        if stow is None:
            return
        try:
            self.servos.write_us(self.calib.servos.shoulder_ch, stow[0])
            self.servos.write_us(self.calib.servos.wrist_ch, stow[1])
        except Exception as e:  # a missing servo must not cost us the survey
            log.warning("could not stow the scoop: %s", e)
            return
        self.sleep(self.calib.servos.ramp_s)

    def _prepare_camera(self) -> None:
        """Focus for the ground, then pin exposure -- and report whether it took.

        A locked exposure is what lets the eight views stitch: on auto, each frame is
        metered differently and the mosaic comes out in visible bands. docs/05 U-21 asks
        whether the driver honours the lock, and camera.py answers it from frame metadata
        rather than assuming, so log the real answer either way.
        """
        self.camera.focus_for_survey()
        exposure, white_balance = self.camera.lock_exposure()
        self._event(
            "camera_locked",
            "",
            {"exposure_locked": bool(exposure), "wb_locked": bool(white_balance)},
        )
        if not exposure:
            log.warning(
                "exposure is not locked; camera.grab() falls back to a %.2f s settle per "
                "view (docs/05 U-21) and the mosaic may band",
                self.calib.camera.settle_s,
            )

    def _capture_views(
        self, n_views: int, step_deg: float
    ) -> tuple[list[StationKeyframe], dict[str, bytes], list[tuple[float, Any]], list[_Raw]]:
        """Photograph `n_views` directions, turning `step_deg` between each."""
        keyframes: list[StationKeyframe] = []
        jpegs: dict[str, bytes] = {}
        views: list[tuple[float, Any]] = []
        raw: list[_Raw] = []

        for i in range(n_views):
            self._check_cut()
            frame = self.camera.grab()
            t = self.clock()
            pose_here = self.pose.current()  # ABSOLUTE heading; see the module docstring
            kf_id = self.ids.next("k")
            name = f"kf_{i:03d}.jpg"
            jpegs[name] = self.camera.jpeg(frame)
            keyframes.append(
                StationKeyframe(kf_id=kf_id, file=name, heading_deg=pose_here.heading_deg, t=t)
            )
            if self.geometry_ok:
                views.append((pose_here.heading_deg, self._warp(frame)))
                raw.extend(self._propose(frame, kf_id, pose_here))

            if i < n_views - 1:
                self._check_cut()
                self.motion.turn(step_deg, self.calib.motion.min_speed)
                self.sleep(self.calib.motion.turn_settle_s)
        return keyframes, jpegs, views, raw

    def _warp(self, frame: Any) -> Any:
        return bev_mod.warp_to_bev(frame, self.calib.homography, self.calib.bev_size_px)

    def _propose(self, frame: Any, kf_id: str, pose_here: Pose) -> list[_Raw]:
        """Rocks in one view, with their mission-frame positions and crops."""
        out: list[_Raw] = []
        for r in prop_mod.find_rocks(frame):
            u, v = r["center_px"]
            fwd, lat = bev_mod.pixel_to_rover_xy(
                u, v, self.calib.homography, self.calib.bev_res_m_px, self.calib.bev_size_px
            )
            xy = rover_to_mission(pose_here, (fwd, lat))
            x, y, w, h = r["bbox_px"]
            out.append(
                _Raw(
                    kf_id=kf_id,
                    bbox_px=[int(x), int(y), int(w), int(h)],
                    xy=(float(xy[0]), float(xy[1])),
                    # The longer bbox edge in metres: a rock's size is what /score's
                    # pickability head needs, and it is only meaningful via the homography.
                    size_m=float(max(w, h) * self.calib.bev_res_m_px),
                    contrast=float(r["contrast"]),
                    crop=self._crop(frame, x, y, w, h),
                )
            )
        return out

    def _crop(self, frame: Any, x: int, y: int, w: int, h: int) -> bytes:
        """A CROP_PX box centred on the bbox, clamped to the frame."""
        fh, fw = frame.shape[:2]
        cx, cy = x + w / 2.0, y + h / 2.0
        half = CROP_PX / 2.0
        x0 = int(max(0, min(fw - CROP_PX, cx - half))) if fw >= CROP_PX else 0
        y0 = int(max(0, min(fh - CROP_PX, cy - half))) if fh >= CROP_PX else 0
        patch = frame[y0 : y0 + CROP_PX, x0 : x0 + CROP_PX]
        return self.camera.jpeg(np.ascontiguousarray(patch))

    def _mosaic(self, views: list[tuple[float, Any]]) -> bytes:
        """The eight BEVs stitched into one tile. PNG, for the alpha channel."""
        import cv2

        tile = bev_mod.stitch_station(views, self.calib.bev_res_m_px, self.calib.bev_size_px)
        ok, buf = cv2.imencode(".png", tile)
        if not ok:
            raise StationError("could not encode bev.png")
        return bytes(buf)

    def _finalize_proposals(self, raw: list[_Raw], files: dict[str, bytes]) -> list[Proposal]:
        """Dedupe across views, cap, then mint p_ ids and attach the crops.

        Ids are allocated last so they are dense: a p_0007 that was deduped away would make
        the dashboard's candidate list look like it lost something.
        """
        kept = self._dedupe(raw)[:MAX_PROPOSALS]
        out: list[Proposal] = []
        for r in kept:
            prop_id = self.ids.next("p")
            crop_name = f"crop_{prop_id}.jpg"
            files[crop_name] = r.crop
            out.append(
                Proposal(
                    prop_id=prop_id,
                    kf_id=r.kf_id,
                    bbox_px=r.bbox_px,
                    crop=crop_name,
                    xy=r.xy,
                    size_m=r.size_m,
                    contrast=r.contrast,
                )
            )
        return out

    @staticmethod
    def _dedupe(raw: list[_Raw]) -> list[_Raw]:
        """One rock seen from two views is one rock. Highest contrast wins."""
        kept: list[_Raw] = []
        for r in sorted(raw, key=lambda p: p.contrast, reverse=True):
            if any((r.xy[0] - k.xy[0]) ** 2 + (r.xy[1] - k.xy[1]) ** 2 < DEDUPE_M**2 for k in kept):
                continue
            kept.append(r)
        return kept

    def _store_and_upload(
        self, station: StationJson, files: dict[str, bytes]
    ) -> MapperStationReply:
        """SD card first, then one upload attempt. A failed upload is still a `done`."""
        assert self.store is not None  # preflight() checked it
        rel = self.store.save_station(station, files)
        if self.uploader is None:
            return self._deferred_reply(station, rel)
        try:
            return self.uploader.upload_station(rel)
        except Exception as e:
            # The bundle is on disk and queued in index.sqlite; the retry thread will send
            # it, surviving a reboot. ok=false is how the brain learns the URLs are not live
            # yet (and gx10/brain/fsm.py reads urls.crops with a "" default, so it scores
            # nothing rather than crashing).
            log.warning("station %s stored but not uploaded: %s", station.station_id, e)
            self._event("station_upload_deferred", station.station_id, {"err": str(e)})
            return self._deferred_reply(station, rel)

    @staticmethod
    def _deferred_reply(station: StationJson, rel: str) -> MapperStationReply:
        """Stored locally, not on the mapper. Says where it went without inventing URLs."""
        return MapperStationReply(
            ok=False,
            station_id=station.station_id,
            stored=f"/data/{rel}/",
            urls=StationUrls(bev="", crops={}),
        )

    # --- plumbing -------------------------------------------------------------------------

    def _check_cut(self) -> None:
        """Abort between views on an E-STOP, a tilt latch, or any motor inhibit.

        `Motion` already polls this inside a turn; this covers the gaps -- a capture, a JPEG
        encode and a settle add up to most of a view's wall time.
        """
        code = self.safety.take_cut()
        if code is not None:
            raise Aborted(code, self.safety.stop_reason() or "cut during station")
        if self.safety.latched():
            raise Aborted("E_ESTOP", self.safety.stop_reason() or "latched during station")
        # Any latch at all, including the one `HardwareBackend.request_stop` sets when the
        # link drops or the op runs out of time. Without this the station would take one more
        # photograph before the next turn noticed, and a station is the one op long enough
        # for that to matter.
        motors = getattr(self.motion, "motors", None)
        if motors is not None and motors.inhibited:
            held = ",".join(sorted(motors.latches))
            raise Aborted("E_TIMEOUT", f"motors inhibited during station ({held})")

    def _event(self, kind: str, ref: str, detail: dict[str, Any]) -> None:
        payload = {"station_id": ref, **detail} if ref else dict(detail)
        log.info("%s %s", kind, payload)
        self.emit(kind, payload)
