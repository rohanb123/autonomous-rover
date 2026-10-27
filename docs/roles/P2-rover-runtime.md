# P2: Rover runtime (hybrid: Pi software + bring-up)

## 1. Goal
Everything that runs **on the Pi**: turn brain commands into safe motion, scoop actions and **station surveys** that produce local maps (keyframes + BEV tile + pose + rock proposals), stored on the SD card and uploaded to the GX10. Plus the **overhead ArUco tracker** on the GX10 and the `fake_rover` mock.

## 2. Scope
> "Not yours" means another person's default area. You may still work on it without asking; warn them first (AGENTS.md §2).

| Yours | Not yours |
|---|---|
| `rover/**` (all Pi code + calibration) except `bev.py` and `proposals.py`, `gx10/overhead/**`, `mocks/fake_rover.py` | Deciding *where* to go or what to sample (brain, P4) |
| WS server :8765 `/rover`, snapshot server :8080 | Scoring/classifying rocks (vision, P3). You only *propose* |
| Motion primitives, heading estimation (gyro + BEV VO), pose, `set_pose` | Global map fusion (mapper, P3) |
| Station capture (calls P3's `bev.py` and `proposals.py`) | DB writes (you never talk to SpacetimeDB) |
| Approach (visual servo), scoop sequence, empty-scoop retry trigger | Physical build (P1) |
| Local store on SD, uploader with retry queue | Ground image `bev.py` and rock proposals `proposals.py`, with their tests (P3 since 2026-10-24, agreed with you) |
| Safety: heartbeat watchdog, estop latch, tilt cutoff | |
| Calibration: motion, camera/BEV homography, scoop poses, scoop zone | |

## 3. Hardware you drive
TB6612 (GPIO 12/5/6 left, 13/20/21 right, STBY 26) · PCA9685 @0x40 (ch0 shoulder, ch1 wrist) · IMU @0x68 (MPU-6050, or kit IMU ⚠️) · ADS1115 @0x48 optional (pot on A0) · LED BCM22 · nav webcam (USB, fixed, 30–35° down) · overhead webcam (USB on the GX10).
**Libraries:** `gpiozero` with the `lgpio` pin factory (Pi 5 has no pigpio support: [pigpio#589](https://github.com/joan2937/pigpio/issues/589)), `adafruit-circuitpython-pca9685` (or `smbus2` direct) ⚠️ pick by I0, `opencv-python-headless` (with `cv2.aruco` ⚠️ check the build includes contrib/aruco on the Pi), `websockets`, `fastapi`/`uvicorn` for :8080, `httpx` for uploads, `pydantic` v2 via `marsrover_shared`.

## 4. Files and function signatures
```python
# rover/rover/config.py  (BUILT)
@dataclass(frozen=True)
class Calib: motor_trim: float; m_per_s_at: Mapping[float,float]; deg_per_s_at: Mapping[float,float]
             homography: tuple[tuple[float,...],...]; bev_res_m_px: float; bev_size_px: int
             scoop_us: Mapping[str, tuple[int,int]]; scoop_zone_px: tuple[int,int,int,int]; present_roi_px: tuple[int,int,int,int]
             # plus nested frozen groups: pins i2c motion camera servos scoop safety pose
             # plus contract_version, calibrated_at (None until calibrate_*.py runs), source
class CalibError(ValueError)
def load_calib(path: str | Path | None = None) -> Calib   # @cache; $ROVER_CALIB; utf-8
def parse_calib(raw: dict, source: Path | None = None) -> Calib   # no IO, for tests
def interp(table: Mapping[float,float], speed: float) -> float    # linear, clamps at the ends

# motors.py  (BUILT)
class PinBackend(Protocol): pwm(pin, duty); out(pin, high); close()
class GpioZeroPins   # real, lazy `import gpiozero`; raises MotorsUnavailable
class RecordingPins  # present=True, records writes -- what `--motors sim` uses
class NullPins       # present=False, discards
class Motors:
    def __init__(self, pins: Pins, trim: float = 0.0, backend: PinBackend | None = None,
                 *, invert_left: bool = False, invert_right: bool = False): ...
    def set(self, left: float, right: float) -> None      # -1..1, trim+invert; STBY high
    def stop(self) -> None                                 # PWM 0, short brake, STBY low
    def inhibit(self, reason: str) -> None                 # set() is a NO-OP while held
    def release(self, reason: str) -> None                 # named, independent latches
    @property inhibited: bool; latches: frozenset[str]; present: bool; last: tuple[float,float]
# Call inhibit() BEFORE stop(): the op worker calls set() every 20 ms, so an un-latched
# stop is undone almost immediately (docs/05 R14).

# servos.py  (BUILT)
class I2cBus(Protocol): read8; read_block; write8; close   # shared with imu.py
class Smbus2Bus        # docs/05 U-23 resolved: plain smbus2 register writes, not Blinka
def prescale_for(freq_hz: float) -> int             # 50 Hz -> 121
def us_to_ticks(us: float, freq_hz: float) -> int   # 1500 us @ 50 Hz -> 307
class Servos:
    def __init__(self, i2c_addr: int = 0x40, freq_hz: int = 50, bus: I2cBus | None = None,
                 *, limits_us: tuple[int,int] = (600, 2400), sleep=None): ...
    def write_us(self, ch: int, us: int) -> None           # clamps to limits_us
    def off(self, ch: int) -> None; def last_us(self, ch) -> int | None
    @property present: bool

# scoop.py
POSES = ("STOW","DEPLOY","CURL","PRESENT","DUMP")
class Scoop:
    def goto(self, pose: str, ramp_s: float = 0.4) -> None
    def sequence_pick(self, motion: "Motion") -> None      # DEPLOY → drive 0.04 m slow → CURL → PRESENT
    def dump(self) -> None                                 # DUMP → STOW

# imu.py
class Imu:
    def detect(self) -> str                                # "mpu6050@0x68" | "adxl345@0x53" | "none"
    def read(self) -> dict                                 # {ax, ay, az, gz_dps?}
    def has_gyro(self) -> bool
    def gyro_bias_calibrate(self, seconds: float = 2.0) -> float

# heading.py
class Heading:
    def integrate_gyro(self, gz_dps: float, dt: float) -> float
    def vo_yaw_deg(self, bev_prev, bev_next) -> tuple[float, float]   # (dyaw_deg, inlier_ratio) via cv2.estimateAffinePartial2D
    def fuse(self, gyro_deg: float, vo_deg: float | None, w_vo: float) -> float

# pose.py  (BUILT)
class PoseEstimator:
    def __init__(self, cfg: PoseCfg, pose: Pose | None = None): ...
    def predict_drive(self, dist_m: float) -> Pose
    def predict_turn(self, dyaw_deg: float) -> Pose            # wraps to (-180, 180]
    def set_pose(self, p: Pose) -> None                        # resets the sigma growth
    def current(self) -> Pose                                  # immutable, replaced under a lock
    def use_gyro(self, available: bool) -> None                # picks "dr" vs "dr+gyro"

# motion.py  (BUILT)
class Aborted(RuntimeError)   # .code: ErrCode -- E_ESTOP / E_TIMEOUT / E_TILT
class Motion:
    def __init__(self, motors, pose, calib, imu=None, safety=None,
                 *, clock=time.monotonic, sleep=time.sleep): ...
    def drive(self, dist_m: float, speed: float) -> Pose            # PID heading hold on the gyro; distance from m_per_s_at
    def turn(self, deg: float, speed: float) -> tuple[Pose, str]    # PID on the gyro, else timed
    def stop(self) -> Pose; def request_stop(self); def clear_stop(self)
# Blocks on the worker thread, polling every calib.safety.stop_tick_s (20 ms) -- that is the
# stop-latency budget. The pose integrates ELAPSED time, so a drive that was cut reports
# where it actually got to. A gyro turn has a wall-clock ceiling, or a dead gyro (reading 0
# forever) would spin until the heartbeat watchdog happened to notice. Both PID loops use
# rover/rover/pid.py (derivative on the gyro rate, clamped integral); gains are
# calib.yaml motion.heading_* and motion.turn_*.

# camera.py  (BUILT)
class FrameSource(Protocol): read; set; get; release
class OpenCvSource      # lazy `import cv2`, MJPG fourcc
class SyntheticSource   # seeded gradient + dark blobs; `--camera sim`, and proposals tests
class NullSource        # present=False
class Camera:
    def open(self, index=None, w=None, h=None) -> None   # locks exposure/WB, then READS IT BACK
    def grab(self) -> np.ndarray                          # flushes calib.camera.flush_frames
    def jpeg(self, img, quality: int | None = None) -> bytes
    @property present: bool
    exposure_locked: bool   # docs/05 U-21: did the driver honour it? verified, not assumed

# bev.py  (P3 since 2026-10-24: these signatures are the interface station.py calls)
def warp_to_bev(img: np.ndarray, H: np.ndarray, size_px: int) -> np.ndarray
def pixel_to_rover_xy(u: float, v: float, H: np.ndarray, res_m_px: float, size_px: int) -> tuple[float,float]
def stitch_station(views: list[tuple[float, np.ndarray]], res_m_px: float, size_px: int) -> np.ndarray  # rotate each BEV by its heading, max-blend

# proposals.py  (P3 since 2026-10-24; "rockster-lite": edges + contours, inspired by Rockster/AEGIS target finding)
def find_rocks(img: np.ndarray, min_area_px: int = 80, max_area_px: int = 8000) -> list[dict]
    # [{bbox_px:[x,y,w,h], contrast: float, solidity: float, center_px:[u,v]}]  Canny → close → findContours → filter

# station.py
def run_station(station_id: str, n_views: int, step_deg: float) -> dict   # returns station.json dict (00 §6.4); keyframes[].heading_deg = absolute mission-frame heading

# approach.py
def approach(cand_xy: tuple[float,float], timeout_s: float = 45) -> dict  # {in_zone, pose, offset_px}

# local_store.py  (BUILT)
class LocalStore:
    def __init__(self, root: str | Path = "data/rover"): ...   # <root>/<run>/stations/s_xxxx/, <root>/<run>/samples/a_xxxx/
    def save_station(self, station: StationJson, files: Mapping[str, bytes]) -> str   # rel path; atomic (tmp dir + rename)
    def save_sample(self, sample: SampleJson, jpg: bytes) -> str
    def load(self, rel: str) -> Bundle; def pending_uploads(self) -> list[str]        # oldest first
    def mark_uploaded / mark_failed / mark_rejected(rel, ...); backlog: int
# The queue IS <root>/index.sqlite, so unsent bundles survive a reboot.

# uploader.py  (BUILT; httpx + pydantic only, fake_rover imports `multipart` from it)
def multipart(meta_name, meta_json, files) -> list[tuple]       # doc 00 6.4-6.5 wire format
class Uploader:
    def __init__(self, store, mapper_url=DEFAULT_MAPPER, emit=None, *, timeout_s=10, ...): ...
    def upload_station(self, rel: str) -> MapperStationReply  # ONE try; UploadFailed -> op still answers done
    def upload_sample(self, rel: str) -> SampleReply
    def start(); def stop(); def retry_loop()                  # thread; backoff 1 s doubling to 30 s
    backlog: int                                               # -> telemetry.uplink_backlog
# A 4xx marks the bundle rejected (kept on disk, not retried) so it can't block the queue.

# snapshot_server.py  (BUILT) -- :8080 on the runtime's own loop; lores stream only, never main
def make_app(camera) -> FastAPI      # /snapshot.jpg, /stream.mjpg (hw MJPEG, else sw loop), /health
async def serve(camera, host, port)  # producer runs only while someone watches

# linkcheck.py  (BUILT) -- python -m rover.linkcheck [--only pi|gx10] ; exit 1 on any FAIL
# Every doc 00 section 3 port, telemetry schema, WS RTT (bad-op probe, side-effect free),
# clock offset NTP-style from the same probe (<= 50 ms), mapper /health.
# Deploy: rover/deploy/ (setup_pi.sh, rover.service, chrony, README).

# safety.py  (BUILT)
def tilt_deg(imu: dict) -> float   # atan2(hypot(ax,ay), az), so upside-down reads ~180 not ~0
class Safety:
    def __init__(self, motors, moving: Callable[[], bool], *, clock=time.monotonic,
                 imu_read=None, on_event=None, heartbeat_timeout_s=1.0,
                 tilt_limit_deg=35.0, tilt_clear_ticks=5): ...
    def on_heartbeat(self) -> None                             # also releases the watchdog latch
    def tick(self) -> None                                     # 20 Hz: heartbeat + tilt cutoffs
    def estop(self) -> None; def clear(self) -> None; def latched(self) -> bool
    def tilt_ok(self, imu: dict | None) -> bool                # True when we cannot tell
    def take_cut(self) -> ErrCode | None                       # the cut that just fired, once
    def stop_reason(self) -> str | None
# The latches clear differently: "estop" only on clear_estop, "watchdog" on the next
# heartbeat (so a reconnecting brain just resumes), "tilt" after N consecutive safe reads.

# ws_server.py  (BUILT -- the ONLY implementation of doc 00 section 6.2. mocks/fake_rover.py
#                 imports it and injects a simulated backend, so both share one dispatch and
#                 rover/tests/test_protocol.py certifies the code that actually ships.)
class Backend(Protocol)   # telemetry/state/estopped, attach/detach, the immediate ops, the
                          # long ops as `async def`, on_heartbeat/watchdog_tick/request_stop
class OpError(Exception)  # .code: ErrCode -> an `err` frame
class OpSlot              # one long op at a time; a second gets E_BUSY
class EventBus            # thread-safe emit (call_soon_threadsafe when off the loop)
class Session             # reader + ack <= 0.5 s + dispatch, 5 Hz telemetry, 20 Hz watchdog
def make_handler(backend: Backend, path: str = "/rover") -> Callable
def serve_rover(backend: Backend, host: str, port: int)
# websockets + pydantic + stdlib ONLY: no cv2/numpy/gpiozero may reach this module, or
# fake_rover stops being runnable on a teammate's laptop. test_ws_server.py asserts it.

# main.py  (BUILT) -- entry point
class HardwareBackend     # implements Backend over motors/servos/camera/imu; long ops run on
                          # one ThreadPoolExecutor worker, stops come from the loop thread
def build_backend(calib: Calib, modes: dict[str, str], *, store_root=None,
                  mapper_url=DEFAULT_MAPPER) -> HardwareBackend
#   python rover/rover/main.py --motors {auto,gpio,sim,off} --servos {auto,pca9685,off}
#                              --imu {auto,off} --camera {auto,opencv,sim,off} --no-hw
#                              --mapper URL --store DIR --snapshot-port 8080 (0 = off)
#   Device choice is CLI/env, never calib.yaml: calibration is measured numbers that are the
#   same on every machine, while which devices exist differs per machine and per hour.
#   `auto` probes, logs an inventory and degrades to off; `sim` must be asked for.

# gx10/overhead/aruco_tracker.py
class ArucoTracker:
    def __init__(self, cam_index: int, dict_name: str = "DICT_4X4_50", marker_len_m: dict[int,float] = ...): ...
    def calibrate_arena(self, corners_xy: dict[int, tuple[float,float]]) -> np.ndarray   # image→mission homography from ids 1–4
    def pose(self) -> dict | None                              # {x,y,heading_deg,reproj_err_px,visible}
# gx10/overhead/app.py: WS :8300 /pose broadcast at 10 Hz (00 §6.12)
```

## 5. How a station works (your core algorithm)
1. Scoop to STOW. Stop. Settle 0.3 s.
2. For i in 0..7: grab frame → save `kf_00i.jpg` (+ its heading: the **absolute mission-frame heading at capture**, wrapped with `wrap_deg`; a station that starts at 30° records 30, 75, 120, …, not 0, 45, 90) → BEV warp → proposals on the raw frame → convert each `center_px` → rover xy → mission xy using the current pose → crop 96×96 around the bbox → turn +45° (gyro-closed) → settle 0.3 s.
3. Between consecutive BEVs, estimate yaw with `vo_yaw_deg`. If the inlier ratio is > 0.5 and it disagrees with the gyro by < 10°, fuse (w_vo = 0.5). Correct the headings of the views.
4. `stitch_station` → `bev.png` (rover frame at the station's start heading).
5. Dedupe proposals within the station (< 0.05 m apart → keep the higher contrast).
6. Save to `LocalStore`, upload (non-blocking: if the upload fails, return `done` anyway with `mapper: {"ok": false}` and queue a retry).
7. Return `StationResult`.

## 6. Calibration (do before I2)
- `calibrate_motion.py`: drive 1.0 m at speeds 0.3/0.5/0.7, measure with tape → `m_per_s_at`; turn 360° → `deg_per_s_at` (timed fallback); trim so a 1 m drive drifts < 5 cm.
- `calibrate_camera.py`: lay a 4-corner paper rectangle of known size on the ground in front of the rover → click corners → `homography` (image px → BEV px). `bev_res_m_px = 0.005`, `bev_size_px = 360` (1.8 m square). Verify: a 10 cm square measures 10 ± 1 cm in BEV.
- `calibrate_scoop.py`: interactive µs jog per pose → `scoop_us`; place a pebble where the scoop lip lands in DEPLOY → record its pixel box as `scoop_zone_px`; PRESENT → record `present_roi_px`.

## 7. Build order, checkpoints, acceptance
| Done by | Build | ✅ Checkpoint / acceptance |
|---|---|---|
| by I0 | Flash Pi OS, Wi-Fi to marsnet, static .20, `pip install -e shared/py`, `mocks/fake_rover.py` (speaks the full WS protocol with canned images) | **I0:** P4's brain connects to fake_rover and to the real Pi WS stub; heartbeat flows |
| by I1 | motors, servos, imu, camera, safety, ws_server | `drive 0.3` and `turn 90` work from a WS client; watchdog stops the motors when you kill the client mid-drive |
| by I2 | calibration, pose, heading (gyro), snapshot server | 360° turn error < 10° (gyro) / < 20° (timed); 1 m drive < 5 cm drift |
| by I2 | station, local_store, uploader (bev + proposals come from P3) | **I1/I2:** a `station` produces 8 keyframes + BEV + proposals on the SD card and in the mapper's `/data/runs/…` (P3) within 60 s |
| by I3 | approach, scoop sequence, overhead tracker | Approach puts the pebble in the scoop zone ≥ 8/10; scoop pick ≥ 7/10 (with P1) |
| by I4 | VO fusion, robustness: reconnect, camera re-open, upload retry | **I3/I4:** full mission end to end with the brain; unplugging Wi-Fi for 10 s → motors stop, then the run resumes after reconnect |
| by I5 | Bug fixes only, tune thresholds | **I5 freeze** |

Unit tests in `rover/tests/`: `test_bev.py` (pixel→xy on a synthetic homography) and `test_proposals.py` (finds 3 dark blobs on a synthetic image), both P3's since 2026-10-24; `test_safety.py` (watchdog timing with a fake clock), `test_protocol.py` (every op round-trips through pydantic).

## 8. Mocks
- **You provide** `mocks/fake_rover.py`: same WS protocol and timing (station ≈ 5 s), poses moving as commanded, station bundles uploaded to the real mapper (or fake_mapper), and `scoop` returning a `present.jpg`. Images are **generated in code** (PIL: sand grain + dark blobs), not read from `mocks/data/stations/` — no image files in git, and `--seed` makes a run replayable. P4 and P3 build against it from I0.
  - Flags: `--host --port --mapper --speed-scale --noise --fail-rate --seed --no-upload`. `--speed-scale` compresses *simulated* time only: the 1.0 s heartbeat watchdog, the 0.5 s ack budget and the per-op deadlines stay in real time, so a client that does not heartbeat still gets its motion cut short.
  - Deliberate differences from the real Pi: no `:8080` snapshot server (`snapshot` returns the documented URL without serving it), `turn` always reports `method:"gyro"`, and `--no-upload` fabricates a successful mapper reply.
  - Contract tests live in `rover/tests/test_protocol.py` and cover every op in `Op`.
- **You use** `mocks/fake_mapper.py` (P3) until the real mapper is up.

## 9. Watch-outs
- **Stale frames**: USB webcams buffer; flush 3 frames before a capture.
- Auto-exposure changes between views break VO and stitching → lock exposure if `cv2.CAP_PROP_AUTO_EXPOSURE` works on this camera ⚠️.
- Don't block the asyncio loop with motion or OpenCV; run ops in a worker thread and keep heartbeat/estop handling on the loop.
- **E-stop must work even while an op runs**: the estop handler sets the latch and calls `Motors.stop()` directly.
- The scoop must be in STOW before every station (or it's in the photos).
- **Keyframe `heading_deg` is absolute** (mission frame, wrapped to (-180, 180]), never relative to the station's start (doc 00 §6.4, contract 1.3.0). The mapper copies it straight into the DB; a relative value silently misplaces every keyframe after the first.
- Station xy depends on heading: a 5° heading error at 1.5 m = 13 cm of target error, which is bigger than the 0.20 m exclusion radius margin you'd like. Fix heading first.
- The Pi clock must be synced (chrony to the GX10) or replay ordering breaks.

## 10. Contracts you need (copies; source of truth = [00](../00-overview-and-contracts.md))
**Ports:** Pi WS `:8765/rover`, snapshot `:8080`; mapper `http://192.168.50.10:8200`; overhead WS `:8300/pose`.
**Frames/units:** mission frame origin = home pad, +x = start heading, +y left, heading CCW+ degrees in (-180, 180]; m, deg, s; `t` = unix float. BEV tiles: rover frame at capture, image up = +x, 0.005 m/px.
**IDs you mint:** `s_0001`, `k_0001`, `p_0001` (4 digits, per run).
**Command envelope:** `{"type":"cmd","t","src":"brain","cmd_id","op","args"}` → `ack` ≤ 0.5 s → `done {result}` or `err {code,msg}`.
**Ops:** `start_run{run_id}`, `drive{dist_m,speed}`, `turn{deg,speed}`, `stop`, `estop`, `clear_estop`, `set_pose{x,y,heading_deg,source}`, `station{station_id,n_views,step_deg}`, `approach{cand_id,x,y}`, `scoop{sample_id}`, `dump`, `scoop_pose{pose}`, `snapshot`, `heartbeat`.
**Errors:** `E_ESTOP E_BUSY E_TIMEOUT E_BAD_ARGS E_TILT E_CAMERA E_LOST_TARGET E_UPLOAD E_SCOOP E_INTERNAL`.
**Telemetry 5 Hz:** `{type:"telemetry", run_id, pose{x,y,heading_deg,source,sigma_xy_m}, state, estop, imu{ax,ay,az,gz_dps}, scoop{pose,us[2]}, cpu_temp_c, queue_len, uplink_backlog}`.
**Station upload:** `POST /ingest/station` multipart: `station.json` (`run_id, station_id, t, pose, keyframes[{kf_id,file,heading_deg (absolute mission frame, wrapped),t}], bev{file,res_m_px,w_px,h_px,frame}, proposals[{prop_id,kf_id,bbox_px,crop,xy,size_m,contrast}]`) + `kf_*.jpg`, `bev.png`, `crop_p_*.jpg`. Reply `{ok, station_id, stored, urls{bev, crops{}}}`.
**Sample upload:** `POST /ingest/sample` multipart: `sample.json {run_id, sample_id, cand_id, t, pose}` + `present.jpg` → `{ok, url}`.
**Timeouts:** heartbeat every 0.25 s; stop if none for 1.0 s while moving; station ≤ 60 s; approach ≤ 45 s; scoop ≤ 30 s.
**Overhead:** `{"type":"overhead_pose","t","src":"overhead","x","y","heading_deg","marker_id":0,"reproj_err_px","visible"}` at 10 Hz.
