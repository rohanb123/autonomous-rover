"""Rover runtime entry point: the real Pi's side of doc 00 section 6.2.

    python rover/rover/main.py                       # :8765, probe every device
    python rover/rover/main.py --no-hw               # laptop: nothing attached
    python rover/rover/main.py --motors sim --camera sim   # laptop, but real timing loops
    python -m rover.main --motors auto --imu auto          # on the Pi, after pip install -e rover

Either form works: like mocks/fake_rover.py, this puts shared/py and rover/ on sys.path
itself, so it runs from a bare checkout with nothing installed.

Device selection is per-device and lives on the CLI, not in calib.yaml: calibration is
measured numbers that are identical on every machine, while *which devices exist* differs
between a laptop and the Pi and changes hourly while P1 builds the harness. `auto` probes,
logs a one-line inventory and degrades to `off` -- it never crashes and never silently
simulates. `sim` has to be asked for.

Env fallbacks, each overridden by its flag: ROVER_MOTORS, ROVER_SONAR, ROVER_IMU,
ROVER_CAMERA, ROVER_CALIB.

A missing device answers `E_INTERNAL` with a message naming it. Not `E_LOST_TARGET`:
P4's FSM maps that onto a *physical* outcome ("unreachable"), so using it would make the
brain record a rock as out of reach when the truth is that a device is unplugged.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):  # run as a file rather than -m, so bootstrap the paths
    sys.path.insert(0, str(_ROOT / "shared" / "py"))
    sys.path.insert(0, str(_ROOT / "rover"))

from marsrover_shared import eventlog  # noqa: E402
from marsrover_shared.ids import IdGen  # noqa: E402
from marsrover_shared.schemas import (  # noqa: E402
    ErrCode,
    Imu,
    Pose,
    RoverState,
    SampleJson,
    Telemetry,
)
from rover import approach as approach_mod  # noqa: E402
from rover import snapshot_server, ws_server  # noqa: E402
from rover.camera import Camera, CameraError, NullSource, SyntheticSource  # noqa: E402
from rover.config import Calib, CalibError, load_calib  # noqa: E402
from rover.i2c import I2cUnavailable, Smbus2Bus  # noqa: E402
from rover.imu import Imu as ImuDevice  # noqa: E402
from rover.imu import ImuPoller  # noqa: E402
from rover.local_store import LocalStore  # noqa: E402
from rover.motion import Aborted, Motion  # noqa: E402
from rover.motors import (  # noqa: E402
    GpioZeroPins,
    Motors,
    MotorsUnavailable,
    NullPins,
    RecordingPins,
    motor_pins,
)
from rover.pose import PoseEstimator  # noqa: E402
from rover.safety import Safety  # noqa: E402
from rover.sonar import Sonar, SonarPoller, SonarUnavailable  # noqa: E402
from rover.station import Station, StationError  # noqa: E402
from rover.uploader import DEFAULT_MAPPER, Uploader  # noqa: E402
from rover.ws_server import EventBus, OpError  # noqa: E402

log = logging.getLogger("rover")

SRC = "rover"
CPU_TEMP_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
CPU_TEMP_CACHE_S = 1.0

#: Duty for the last, short `approach` moves: slow enough not to overshoot a 30 cm gap.
APPROACH_SPEED = 0.4
#: Below this an approach drive is skipped; timed drives cannot resolve it.
MIN_APPROACH_DRIVE_M = 0.01


# --- device factories ----------------------------------------------------------------------


def make_motors(mode: str, calib: Calib) -> Motors:
    """`auto` claims the pins even with no harness attached, which is how you probe wiring."""
    kwargs: dict[str, Any] = {
        "trim": calib.motor_trim,
        "invert_left": calib.motion.invert_left,
        "invert_right": calib.motion.invert_right,
    }
    if mode == "off":
        return Motors(calib.pins, backend=NullPins(), **kwargs)
    if mode == "sim":
        return Motors(calib.pins, backend=RecordingPins(), **kwargs)
    pwm, out = motor_pins(calib.pins)
    try:
        return Motors(calib.pins, backend=GpioZeroPins(pwm, out), **kwargs)
    except MotorsUnavailable as e:
        if mode == "gpio":
            raise
        log.warning("motors: no GPIO (%s); continuing with none attached", e)
        return Motors(calib.pins, backend=NullPins(), **kwargs)


def make_imu(mode: str, calib: Calib) -> ImuDevice | None:
    """Probe the I2C bus for an MPU-6050, else an ADXL345, else nothing.

    A gyro is what lets `Motion.turn` close the loop instead of trusting a timing table,
    which is docs/05 U-2 and the mitigation for R2. With no gyro, turns stay timed and the
    tilt cutoff is disabled rather than faked.
    """
    if mode == "off":
        log.warning("--imu off: turns stay timed and the tilt cutoff is disabled")
        return None
    wanted = {"mpu6050": (0x68,), "adxl345": (0x53,)}.get(mode, calib.i2c.imu_addrs)
    try:
        bus = Smbus2Bus(calib.i2c.bus)
    except I2cUnavailable as e:
        if mode != "auto":
            raise SystemExit(f"--imu {mode}: {e}") from e
        log.warning("imu: no I2C bus (%s); turns stay timed, tilt cutoff off", e)
        return None
    imu = ImuDevice(bus, tuple(wanted))
    if not imu.present:
        if mode != "auto":
            raise SystemExit(f"--imu {mode}: nothing at {[hex(a) for a in wanted]}")
        imu.close()
        return None
    return imu


def make_sonar(mode: str, calib: Calib) -> Sonar:
    """Probe the HC-SR04. Absent is normal and not an error (contract 2.0.0).

    The range is a telemetry field and situational awareness, not a safety interlock --
    nothing in the runtime brakes on it, and an HC-SR04 does not see soft or angled surfaces
    well enough to be trusted with that.
    """
    if mode == "off":
        return Sonar()
    try:
        sonar = Sonar.open(calib.pins.sonar_trig, calib.pins.sonar_echo)
    except SonarUnavailable as e:
        if mode == "gpio":
            raise
        log.warning("sonar: none usable (%s); range_m will be null", e)
        return Sonar()
    log.info("sonar: %s on GPIO %d/%d", sonar.name, calib.pins.sonar_trig, calib.pins.sonar_echo)
    return sonar


def make_camera(mode: str, calib: Calib) -> Camera:
    """`auto` prefers the CSI Camera Module 3, then a USB camera, then none."""
    if mode == "off":
        return Camera(calib.camera, NullSource())
    if mode == "sim":
        return Camera(calib.camera, SyntheticSource(calib.camera.width, calib.camera.height))
    camera = Camera(calib.camera)
    try:
        camera.open(backend=mode)
    except CameraError as e:
        if mode in ("picamera2", "opencv"):
            raise
        log.warning("camera: none usable (%s); continuing without one", e)
        return Camera(calib.camera, NullSource())
    log.info("camera: %s", camera.status())
    return camera


# --- the backend ---------------------------------------------------------------------------


class HardwareBackend:
    """`ws_server.Backend` over the real hardware. Long ops run on one worker thread.

    Everything the asyncio loop reads (pose, range, state) is either an immutable value
    replaced wholesale or a plain bool, so telemetry never needs a lock. Everything that
    blocks (motion, capture) goes through `_offload`.
    """

    def __init__(
        self,
        calib: Calib,
        motors: Motors,
        camera: Camera,
        imu: ImuDevice | None = None,
        sonar: Sonar | None = None,
        store: LocalStore | None = None,
        mapper_url: str = DEFAULT_MAPPER,
        allow_uncalibrated: bool = False,
    ) -> None:
        self.calib = calib
        # Bench mode: an uncalibrated station uploads keyframes only (station.py).
        self.allow_uncalibrated = allow_uncalibrated
        self.motors = motors
        self.camera = camera
        self.imu = imu
        self.sonar = sonar
        # Same reason as the IMU's poller: a read blocks and telemetry runs on the loop.
        self.sonar_poller = SonarPoller(sonar) if sonar is not None and sonar.present else None

        self.run_id = ""
        self.ids = IdGen()
        self.last_cand_id = ""
        self._state: RoverState = "IDLE"
        self._estop = False
        self._cpu_temp = (0.0, 0.0)  # (value, read_at)

        self.bus = EventBus()
        self.pose = PoseEstimator(calib.pose)
        self.pose.use_gyro(bool(imu is not None and imu.has_gyro()))
        # The poller is what the loop thread reads; Motion keeps the live device, since
        # it runs on the worker thread and a gyro turn wants the freshest sample.
        self.poller = ImuPoller(imu) if imu is not None else None
        self.safety = Safety(
            motors,
            moving=lambda: self._state in ws_server.MOVING_STATES,
            imu_read=(self.poller.cached if self.poller is not None else None),
            on_event=self.bus.emit,
            heartbeat_timeout_s=calib.safety.heartbeat_timeout_s,
            tilt_limit_deg=calib.safety.tilt_limit_deg,
            tilt_clear_ticks=calib.safety.tilt_clear_ticks,
        )
        self.motion = Motion(motors, self.pose, calib, imu=imu, safety=self.safety)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rover-op")
        # No store = no uploads (tests, mostly). With one, bundles queue on the SD card and
        # a failed upload becomes an `upload_retry` event plus a retry from its own thread.
        self.store = store
        self.uploader = Uploader(store, mapper_url, self.bus.emit) if store is not None else None
        self.snapshot_served = False  # main_async flips it once :8080 is being served

    # --- observable state ----------------------------------------------------------------

    @property
    def state(self) -> RoverState:
        return self._state

    @property
    def estopped(self) -> bool:
        return self._estop

    def attach(self, queue: asyncio.Queue) -> None:
        self.bus.attach(queue)

    def detach(self, queue: asyncio.Queue) -> None:
        self.bus.detach(queue)

    def _cpu_temp_c(self) -> float:
        value, read_at = self._cpu_temp
        now = time.monotonic()
        if now - read_at < CPU_TEMP_CACHE_S:
            return value
        try:  # millidegrees on the Pi; absent on Windows
            value = int(CPU_TEMP_PATH.read_text(encoding="utf-8").strip()) / 1000.0
        except (OSError, ValueError):
            value = 0.0
        self._cpu_temp = (value, now)
        return value

    def _imu_reading(self) -> Imu:
        """From the poller cache: this runs on the event loop, which must not block."""
        if self.poller is not None:
            cached = self.poller.cached()
            if cached is not None:
                return Imu(
                    ax=float(cached.get("ax", 0.0)),
                    ay=float(cached.get("ay", 0.0)),
                    az=float(cached.get("az", 0.0)),
                    gz_dps=float(cached.get("gz_dps", 0.0)),
                )
        if self.imu is None:
            # All zeros, not a fabricated 9.79: a reader can tell there is no accelerometer.
            return Imu(ax=0.0, ay=0.0, az=0.0, gz_dps=0.0)
        r = self.imu.read()
        return Imu(
            ax=float(r.get("ax", 0.0)),
            ay=float(r.get("ay", 0.0)),
            az=float(r.get("az", 0.0)),
            gz_dps=float(r.get("gz_dps", 0.0)),
        )

    def _range_m(self) -> float | None:
        """From the poller cache. None means no reading, which is not the same as 0.0 m."""
        return self.sonar_poller.cached() if self.sonar_poller is not None else None

    def telemetry(self) -> Telemetry:
        return Telemetry(
            run_id=self.run_id,
            pose=self.pose.current(),
            state=self._state,
            estop=self._estop,
            imu=self._imu_reading(),
            range_m=self._range_m(),
            cpu_temp_c=self._cpu_temp_c(),
            queue_len=1 if self._state not in ("IDLE", "ESTOP") else 0,
            # A COUNT over a few dozen index rows: well under a millisecond on the loop.
            uplink_backlog=self.uploader.backlog if self.uploader is not None else 0,
        )

    # --- events --------------------------------------------------------------------------

    def _log_event(self, level: str, kind: str, msg: str, data: dict[str, Any]) -> None:
        """events.jsonl only. Event.kind is a closed Literal with no value for these."""
        if not self.run_id:
            return
        with contextlib.suppress(OSError):
            eventlog.log(self.run_id, SRC, level, kind, msg, data)

    def _need(self, op: str, device: str, present: bool, hint: str) -> None:
        if present:
            return
        msg = f"{op} needs the {device}, which is not attached ({hint})"
        log.error("%s", msg)
        self._log_event("warn", "no_device", msg, {"op": op, "device": device})
        raise OpError("E_INTERNAL", msg)

    # --- immediate ops -------------------------------------------------------------------

    def start_run(self, run_id: str) -> dict[str, Any]:
        self.run_id = run_id
        self.ids = IdGen()  # k_/p_ counters are per run (doc 00 section 5)
        self.pose.set_pose(Pose(x=0.0, y=0.0, heading_deg=0.0, source="dr"))
        if self.imu is not None and self.imu.has_gyro():
            # The rover is stationary at the start of a run, which is the only time this
            # is measurable. A degree per second of bias is 4 degrees over a station spin.
            bias = self.imu.gyro_bias_calibrate()
            self._log_event("info", "gyro_bias", f"gyro bias {bias:.3f} deg/s", {"dps": bias})
        self._log_event("info", "hw_inventory", "rover runtime up", self.inventory())
        log.info("run %s started; %s", run_id, self.inventory())
        return {"ok": True}

    def set_pose(self, x: float, y: float, heading_deg: float, source: str) -> dict[str, Any]:
        self.pose.set_pose(Pose(x=x, y=y, heading_deg=heading_deg, source=source))  # type: ignore[arg-type]
        return {"pose": self.pose.current().model_dump()}

    def stop(self) -> dict[str, Any]:
        # Cooperative: the running op notices within one stop_tick_s and ends with `done`,
        # and its own `finally` is what returns the state to IDLE.
        return {"pose": self.motion.stop().model_dump()}

    def estop(self) -> dict[str, Any]:
        # Ordering is the point: latch off first, then cut power. Otherwise the worker
        # thread's next set() re-enables the motors about 20 ms later. docs/05 R14.
        self._estop = True
        self._state = "ESTOP"
        self.safety.estop()
        self.motion.request_stop()
        self._log_event("error", "estop", "E-STOP latched", {})
        return {"latched": True}

    def clear_estop(self) -> dict[str, Any]:
        self._estop = False
        self._state = "IDLE"
        self.safety.clear()
        self.motion.clear_stop()
        self._log_event("info", "estop_clear", "E-STOP released", {})
        return {"latched": False}

    def snapshot(self) -> dict[str, Any]:
        """The URL only (doc 00 section 6.2); the brain or dashboard fetches the image itself."""
        self._need("snapshot", "camera", self.camera.present, "use --camera sim on a laptop")
        if not self.snapshot_served:
            msg = "snapshot needs the :8080 server, which is disabled (--snapshot-port 0)"
            log.error("%s", msg)
            raise OpError("E_INTERNAL", msg)
        return {"url": ws_server.SNAPSHOT_URL}

    # --- long ops ------------------------------------------------------------------------

    async def _offload(self, state: RoverState, fn: Any, *args: Any) -> Any:
        """Run a blocking primitive on the single op thread, holding `state` while it runs."""
        loop = asyncio.get_running_loop()
        self._state = state
        try:
            return await loop.run_in_executor(self._pool, fn, *args)
        except Aborted as e:
            # Motion speaks in contract codes already; without this translation ws_server
            # would see a bare RuntimeError and answer E_INTERNAL instead of E_ESTOP.
            raise OpError(e.code, e.msg) from e
        finally:
            if not self._estop:
                self._state = "IDLE"

    async def drive(self, dist_m: float, speed: float) -> dict[str, Any]:
        self._need("drive", "motors", self.motors.present, "use --motors sim to exercise timing")
        pose = await self._offload("MOVING", self.motion.drive, dist_m, speed)
        return {"pose": pose.model_dump()}

    async def turn(self, deg: float, speed: float) -> dict[str, Any]:
        self._need("turn", "motors", self.motors.present, "use --motors sim to exercise timing")
        pose, method = await self._offload("MOVING", self.motion.turn, deg, speed)
        return {"pose": pose.model_dump(), "method": method}

    async def station(self, station_id: str, n_views: int, step_deg: float) -> dict[str, Any]:
        if not self.run_id:
            raise OpError("E_BAD_ARGS", "station before start_run: the bundle needs a run_id")
        if n_views <= 0:
            raise OpError("E_BAD_ARGS", f"n_views must be positive, got {n_views}")
        self._need("station", "camera", self.camera.present, "use --camera sim on a laptop")
        runner = Station(
            self.calib,
            self.camera,
            self.motion,
            self.pose,
            self.safety,
            store=self.store,
            uploader=self.uploader,
            ids=self.ids,  # k_ counters continue across the run's stations (doc 00 section 5)
            emit=self._station_event,
            allow_uncalibrated=self.allow_uncalibrated,
        )
        runner.run_id = self.run_id
        try:
            runner.preflight()  # before anything moves: no store, or uncalibrated
        except StationError as e:
            log.error("station refused: %s", e)
            raise OpError("E_INTERNAL", str(e)) from e
        try:
            return await self._offload("STATION", runner.run_station, station_id, n_views, step_deg)
        except CameraError as e:
            raise OpError("E_CAMERA", str(e)) from e

    def _station_event(self, kind: str, detail: dict[str, Any]) -> None:
        """station.py's progress -> events.jsonl. Not the WS: Event.kind is a closed set."""
        self._log_event("info", kind, f"{kind} {detail}", detail)

    async def approach(self, cand_id: str, x: float, y: float) -> dict[str, Any]:
        """Face (x, y) and close to close-up range. Dead reckoning: see approach.py."""
        self.last_cand_id = cand_id  # sample.json needs it (doc 00 section 6.5)
        self._need("approach", "motors", self.motors.present, "use --motors sim to exercise timing")
        pose = await self._offload("APPROACH", self._approach_moves, x, y)
        return {
            "in_zone": approach_mod.in_zone(pose, x, y),
            "pose": pose.model_dump(),
            "offset_px": [0.0, 0.0],  # UNVERIFIED: no visual servo, so nothing is measured
        }

    def _approach_moves(self, x: float, y: float) -> Pose:
        plan = approach_mod.plan_approach(self.pose.current(), x, y)
        if not plan.reachable:
            log.warning("approach: (%.2f, %.2f) is out of range; not moving", x, y)
            return self.pose.current()
        if plan.turn_deg:
            self.motion.turn(plan.turn_deg, APPROACH_SPEED)
        if abs(plan.drive_m) > MIN_APPROACH_DRIVE_M:
            self.motion.drive(plan.drive_m, APPROACH_SPEED)
        return self.pose.current()

    async def sample(self, sample_id: str) -> dict[str, Any]:
        """A close-up photo of the target, saved to the SD card, then one upload attempt.

        Like a station, a failed upload is still `done` (uploaded=false): the photo is safe
        and the retry thread has it. image_url is "" then, since nothing can fetch it yet.
        """
        if not self.run_id:
            raise OpError("E_BAD_ARGS", "sample before start_run: sample.json needs a run_id")
        self._need("sample", "camera", self.camera.present, "use --camera sim on a laptop")
        if self.store is None:
            raise OpError("E_INTERNAL", "sample needs the local store (--store)")
        try:
            jpg = await self._offload("SAMPLING", self._closeup)
        except CameraError as e:
            raise OpError("E_CAMERA", str(e)) from e
        sample = SampleJson(
            run_id=self.run_id,
            sample_id=sample_id,
            cand_id=self.last_cand_id,
            t=time.time(),
            pose=self.pose.current(),
        )
        rel = await asyncio.to_thread(self.store.save_sample, sample, jpg)  # off the loop
        url, uploaded = "", False
        if self.uploader is not None:
            try:
                url, uploaded = (
                    await asyncio.to_thread(self.uploader.upload_sample, rel)
                ).url, True
            except Exception as e:  # stored and queued: the retry thread sends it later
                log.warning("sample %s stored but not uploaded: %s", sample_id, e)
                self._log_event("warn", "sample_upload_deferred", str(e), {"sample_id": sample_id})
        return {"image_url": url, "uploaded": uploaded, "pose": sample.pose.model_dump()}

    def _closeup(self) -> bytes:
        self.camera.focus_for_present()
        try:
            return self.camera.jpeg(self.camera.grab())
        finally:
            self.camera.focus_for_survey()  # the next station must not inherit macro focus

    # --- safety hooks --------------------------------------------------------------------

    def on_heartbeat(self) -> None:
        self.safety.on_heartbeat()

    def watchdog_tick(self) -> ErrCode | None:
        self.safety.tick()
        return self.safety.take_cut()

    def request_stop(self, code: ErrCode | None = None) -> None:
        """Halt whatever is running. Called on an op deadline and on a dropped link.

        The `inhibit` is the load-bearing half, and it has to be a latch rather than a bare
        `stop()`: a multi-step op is not stopped by stopping the motors once. `station` calls
        `Motion.turn` per view, and `turn` begins with `clear_stop()` -- so without a latch
        the next view erases the stop request and the rover finishes its spin with nobody
        watching, because the 20 Hz watchdog loop dies with the session that asked it to stop.

        The reason is "watchdog" deliberately, not a new name: `Motion._cut` already maps
        that latch to E_TIMEOUT, and `Safety.on_heartbeat` already releases it, so a
        reconnecting brain's first heartbeat un-bricks the rover. A bespoke reason would
        have nothing wired to release it.
        """
        self.motors.inhibit("watchdog")
        self.motion.request_stop()
        self.motors.stop()

    # --- lifecycle -----------------------------------------------------------------------

    def inventory(self) -> dict[str, Any]:
        return {
            "motors": self.motors.present,
            "sonar": self.sonar.present if self.sonar is not None else False,
            "imu": self.imu.detect() if self.imu is not None else False,
            "gyro": self.imu.has_gyro() if self.imu is not None else False,
            "camera": self.camera.backend if self.camera.present else False,
            "exposure_locked": self.camera.exposure_locked,
            "focus_locked": self.camera.focus_locked,
            "calibrated": self.calib.calibrated,
            # Will a `station` work right now, and will it carry geometry when it does?
            # Logged at start_run, so "no proposals" is explainable after the fact rather
            # than looking like the arena had no rocks in it.
            "station": self.store is not None and self.camera.present,
            "station_geometry": self.calib.calibrated,
        }

    def close(self) -> None:
        """Leave the hardware safe: stop the motors, drop the pool."""
        log.info("shutting down")
        if self.poller is not None:
            with contextlib.suppress(Exception):
                self.poller.stop()
        if self.sonar_poller is not None:
            with contextlib.suppress(Exception):
                self.sonar_poller.stop()
        with contextlib.suppress(Exception):
            self.motion.request_stop()
        with contextlib.suppress(Exception):
            self.motors.close()
        with contextlib.suppress(Exception):
            self.camera.close()
        if self.imu is not None:
            with contextlib.suppress(Exception):
                self.imu.close()
        if self.sonar is not None:
            with contextlib.suppress(Exception):
                self.sonar.close()
        if self.uploader is not None:
            with contextlib.suppress(Exception):
                self.uploader.stop()
        if self.store is not None:
            with contextlib.suppress(Exception):
                self.store.close()
        self._pool.shutdown(wait=False, cancel_futures=True)


# --- wiring --------------------------------------------------------------------------------


def build_backend(
    calib: Calib,
    modes: dict[str, str],
    *,
    store_root: str | Path | None = None,
    mapper_url: str = DEFAULT_MAPPER,
    allow_uncalibrated: bool = False,
) -> HardwareBackend:
    motors = make_motors(modes["motors"], calib)
    imu = make_imu(modes["imu"], calib)
    camera = make_camera(modes["camera"], calib)
    sonar = make_sonar(modes["sonar"], calib)
    store = LocalStore(store_root) if store_root is not None else None
    backend = HardwareBackend(
        calib, motors, camera, imu, sonar, store, mapper_url, allow_uncalibrated
    )
    if backend.poller is not None:
        backend.poller.start()
    if backend.sonar_poller is not None:
        backend.sonar_poller.start()
    if backend.uploader is not None:
        # Starts draining whatever an earlier boot left unsent, before any new station.
        backend.uploader.start()
        pending = backend.uploader.backlog
        log.info("uploads -> %s; %d pending in %s", mapper_url, pending, store_root)
    log.info("inventory: %s", backend.inventory())
    if not calib.calibrated:
        log.warning(
            "calib.yaml has calibrated_at: null -- every motion number is a placeholder, "
            "so distances and headings will be wrong until calibrate_*.py has run"
        )
    return backend


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env = os.environ.get
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--host", default="0.0.0.0", help="bind address (default: all interfaces)")
    p.add_argument("--port", type=int, default=8765, help="WS port (doc 00 section 3: 8765)")
    p.add_argument("--calib", default=None, help="calib.yaml path (default: $ROVER_CALIB)")
    p.add_argument(
        "--motors",
        choices=("auto", "gpio", "sim", "off"),
        default=env("ROVER_MOTORS", "auto"),
        help="auto probes gpiozero and degrades to off; sim runs the real timing loop",
    )
    p.add_argument(
        "--sonar",
        choices=("auto", "gpio", "off"),
        default=env("ROVER_SONAR", "auto"),
        help="HC-SR04 forward range (contract 2.0.0); absent is fine, range_m goes null",
    )
    p.add_argument(
        "--imu",
        choices=("auto", "mpu6050", "adxl345", "off"),
        default=env("ROVER_IMU", "auto"),
        help="auto probes 0x68 then 0x53; a named chip fails loudly if absent",
    )
    p.add_argument(
        "--camera",
        choices=("auto", "picamera2", "opencv", "sim", "off"),
        default=env("ROVER_CAMERA", "auto"),
        help="picamera2 = the CSI Camera Module 3; opencv = a USB camera",
    )
    p.add_argument("--no-hw", action="store_true", help="shorthand for every device off")
    p.add_argument(
        "--mapper",
        default=env("ROVER_MAPPER", DEFAULT_MAPPER),
        help=f"mapper base URL for /ingest/* (default {DEFAULT_MAPPER})",
    )
    p.add_argument(
        "--snapshot-port",
        type=int,
        default=int(env("ROVER_SNAPSHOT_PORT", str(snapshot_server.PORT))),
        help="HTTP /snapshot.jpg + /stream.mjpg (doc 00 section 3: 8080); 0 disables",
    )
    p.add_argument(
        "--store",
        default=env("ROVER_STORE", str(_ROOT / "data" / "rover")),
        help="SD-card bundle store + upload queue (default: <repo>/data/rover, gitignored)",
    )
    p.add_argument(
        "--allow-uncalibrated",
        action="store_true",
        default=env("ROVER_ALLOW_UNCALIBRATED", "") == "1",
        help="bench mode: before calibrate_camera.py, a station uploads keyframes only "
        "(no BEV, no proposals) instead of refusing",
    )
    p.add_argument("--log-level", default=env("ROVER_LOG_LEVEL", "INFO"))
    return p.parse_args(argv)


def modes_from(args: argparse.Namespace) -> dict[str, str]:
    if args.no_hw:
        return dict.fromkeys(("motors", "sonar", "imu", "camera"), "off")
    return {
        "motors": args.motors,
        "sonar": args.sonar,
        "imu": args.imu,
        "camera": args.camera,
    }


def _snapshot_ended(task: asyncio.Task, backend: HardwareBackend) -> None:
    """:8080 is debug-only: if it dies (port taken, say), log it and keep the rover running."""
    backend.snapshot_served = False
    if not task.cancelled() and task.exception() is not None:
        log.error("snapshot server stopped: %s", task.exception())


async def _serve_until_signalled() -> None:
    """Block until SIGTERM/SIGINT, then return so the `finally` can shut the hardware down.

    This is why it exists rather than awaiting a bare future: systemd's default kill signal
    is SIGTERM, and Python's default SIGTERM disposition kills the interpreter WITHOUT
    unwinding. So `systemctl stop rover` used to skip `backend.close()` entirely -- no
    `motors.stop()` -- and the kernel released the GPIO pins to floating inputs, which an
    L293D's TTL inputs can read as HIGH (see rover/deploy/rover.service). Ctrl-C was handled
    and `systemctl stop` was not, which is the wrong way round for the one that runs
    unattended.

    `add_signal_handler` is POSIX-only; on Windows it raises NotImplementedError and the
    laptop path falls back to the plain future, where KeyboardInterrupt already works.
    """
    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    import signal

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, lambda n=name: _signalled(stop, n))
        except (NotImplementedError, RuntimeError):
            pass  # Windows, or no main thread: KeyboardInterrupt covers it
    await stop


def _signalled(stop: asyncio.Future, name: str) -> None:
    log.info("%s: shutting down and de-energising the hardware", name)
    if not stop.done():
        stop.set_result(None)


async def main_async(args: argparse.Namespace) -> None:
    calib = load_calib(args.calib)
    backend = build_backend(
        calib,
        modes_from(args),
        store_root=args.store,
        mapper_url=args.mapper,
        allow_uncalibrated=args.allow_uncalibrated,
    )
    http: asyncio.Task | None = None
    try:
        async with ws_server.serve_rover(backend, args.host, args.port) as server:
            port = server.sockets[0].getsockname()[1] if server.sockets else args.port
            log.info("rover runtime on ws://%s:%d%s", args.host, port, ws_server.PATH)
            if args.snapshot_port:
                http = asyncio.create_task(
                    snapshot_server.serve(backend.camera, args.host, args.snapshot_port),
                    name="snapshot-http",
                )
                backend.snapshot_served = True
                http.add_done_callback(lambda t: _snapshot_ended(t, backend))
                log.info("snapshot/stream on http://%s:%d", args.host, args.snapshot_port)
            await _serve_until_signalled()
    finally:
        if http is not None:
            http.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await http
        backend.close()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(main_async(args))
    except CalibError as e:
        raise SystemExit(f"calibration: {e}") from e


if __name__ == "__main__":
    main()
