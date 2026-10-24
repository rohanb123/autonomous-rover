# P2: Rover runtime (hybrid: Pi software + bring-up)

## 1. Goal
Everything that runs **on the Pi**: turn brain commands into safe motion, scoop actions and **station surveys** that produce local maps (keyframes + BEV tile + pose + rock proposals), stored on the SD card and uploaded to the GX10. Plus the **overhead ArUco tracker** on the GX10 and the `fake_rover` mock.

## 2. Scope
| Yours | Not yours |
|---|---|
| `rover/**` (all Pi code + calibration), `gx10/overhead/**`, `mocks/fake_rover.py` | Deciding *where* to go or what to sample (brain, P4) |
| WS server :8765 `/rover`, snapshot server :8080 | Scoring/classifying rocks (vision, P3). You only *propose* |
| Motion primitives, heading estimation (gyro + BEV VO), pose, `set_pose` | Global map fusion (mapper, P3) |
| Station capture, BEV homography, rock proposals ("rockster-lite") | DB writes (you never talk to SpacetimeDB) |
| Approach (visual servo), scoop sequence, empty-scoop retry trigger | Physical build (P1) |
| Local store on SD, uploader with retry queue | |
| Safety: heartbeat watchdog, estop latch, tilt cutoff | |
| Calibration: motion, camera/BEV homography, scoop poses, scoop zone | |

## 3. Hardware you drive
TB6612 (GPIO 12/5/6 left, 13/20/21 right, STBY 26) · PCA9685 @0x40 (ch0 shoulder, ch1 wrist) · IMU @0x68 (MPU-6050, or kit IMU ⚠️) · ADS1115 @0x48 optional (pot on A0) · LED BCM22 · nav webcam (USB, fixed, 30–35° down) · overhead webcam (USB on the GX10).
**Libraries:** `gpiozero` with the `lgpio` pin factory (Pi 5 has no pigpio support: [pigpio#589](https://github.com/joan2937/pigpio/issues/589)), `adafruit-circuitpython-pca9685` (or `smbus2` direct) ⚠️ pick at H2, `opencv-python-headless` (with `cv2.aruco` ⚠️ check the build includes contrib/aruco on the Pi), `websockets`, `fastapi`/`uvicorn` for :8080, `httpx` for uploads, `pydantic` v2 via `marsrover_shared`.

## 4. Files and function signatures
```python
# rover/rover/config.py
@dataclass
class Calib: motor_trim: float; m_per_s_at: dict[float,float]; deg_per_s_at: dict[float,float]
             homography: list[list[float]]; bev_res_m_px: float; bev_size_px: int
             scoop_us: dict[str, tuple[int,int]]; scoop_zone_px: tuple[int,int,int,int]; present_roi_px: tuple[int,int,int,int]
def load_calib(path: str) -> Calib

# motors.py
class Motors:
    def __init__(self, pins: dict, trim: float): ...
    def set(self, left: float, right: float) -> None      # -1..1, applies trim; STBY high
    def stop(self) -> None                                 # brake, STBY low

# servos.py
class Servos:
    def __init__(self, i2c_addr: int = 0x40, freq_hz: int = 50): ...
    def write_us(self, ch: int, us: int) -> None           # clamps to calib limits
    def off(self, ch: int) -> None

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

# pose.py
class PoseEstimator:
    def predict_drive(self, dist_m: float) -> Pose
    def predict_turn(self, dyaw_deg: float) -> Pose
    def set_pose(self, p: Pose) -> None
    def current(self) -> Pose

# motion.py
class Motion:
    def drive(self, dist_m: float, speed: float) -> Pose            # timed by calib m_per_s; stops on estop
    def turn(self, deg: float, speed: float) -> tuple[Pose, str]    # gyro-closed if has_gyro else timed
    def stop(self) -> Pose

# camera.py
class Camera:
    def open(self, index: int = 0, w: int = 640, h: int = 480) -> None   # fixed exposure/white balance if supported
    def grab(self) -> np.ndarray                                         # flush 3 stale frames first
    def jpeg(self, img, quality: int = 85) -> bytes

# bev.py
def warp_to_bev(img: np.ndarray, H: np.ndarray, size_px: int) -> np.ndarray
def pixel_to_rover_xy(u: float, v: float, H: np.ndarray, res_m_px: float, size_px: int) -> tuple[float,float]
def stitch_station(views: list[tuple[float, np.ndarray]], res_m_px: float, size_px: int) -> np.ndarray  # rotate each BEV by its heading, max-blend

# proposals.py  ("rockster-lite": edges + contours, inspired by Rockster/AEGIS target finding)
def find_rocks(img: np.ndarray, min_area_px: int = 80, max_area_px: int = 8000) -> list[dict]
    # [{bbox_px:[x,y,w,h], contrast: float, solidity: float, center_px:[u,v]}]  Canny → close → findContours → filter

# station.py
def run_station(station_id: str, n_views: int, step_deg: float) -> dict   # returns station.json dict (00 §6.4)

# approach.py
def approach(cand_xy: tuple[float,float], timeout_s: float = 45) -> dict  # {in_zone, pose, offset_px}

# local_store.py
class LocalStore:
    def __init__(self, root: str = "runs"): ...                # runs/<run>/stations/s_xxxx/{station.json,kf_*.jpg,bev.png,crop_*.jpg}
    def save_station(self, run_id: str, station: dict, files: dict[str, bytes]) -> str
    def save_sample(self, run_id: str, sample: dict, jpg: bytes) -> str
    def pending_uploads(self) -> list[str]

# uploader.py
class Uploader:
    def upload_station(self, path: str) -> dict                # POST mapper /ingest/station; returns mapper reply
    def upload_sample(self, path: str) -> dict
    def retry_loop(self) -> None                               # background; exponential backoff, max 30 s

# safety.py
class Safety:
    def on_heartbeat(self) -> None
    def tick(self) -> None                                     # 20 Hz: no heartbeat for 1.0 s while moving → stop
    def estop(self) -> None; def clear(self) -> None; def latched(self) -> bool
    def tilt_ok(self, imu: dict) -> bool                       # |tilt| > 35° → stop + event "tilt"

# ws_server.py
async def handle(ws) -> None   # parse cmd (pydantic), ack ≤ 0.5 s, run op in a worker, send done/err; telemetry task 5 Hz

# gx10/overhead/aruco_tracker.py
class ArucoTracker:
    def __init__(self, cam_index: int, dict_name: str = "DICT_4X4_50", marker_len_m: dict[int,float] = ...): ...
    def calibrate_arena(self, corners_xy: dict[int, tuple[float,float]]) -> np.ndarray   # image→mission homography from ids 1–4
    def pose(self) -> dict | None                              # {x,y,heading_deg,reproj_err_px,visible}
# gx10/overhead/app.py: WS :8300 /pose broadcast at 10 Hz (00 §6.12)
```

## 5. How a station works (your core algorithm)
1. Scoop to STOW. Stop. Settle 0.3 s.
2. For i in 0..7: grab frame → save `kf_00i.jpg` (+ heading) → BEV warp → proposals on the raw frame → convert each `center_px` → rover xy → mission xy using the current pose → crop 96×96 around the bbox → turn +45° (gyro-closed) → settle 0.3 s.
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
| When | Build | ✅ Checkpoint / acceptance |
|---|---|---|
| H0–H2 | Flash Pi OS, Wi-Fi to marsnet, static .20, `pip install -e shared/py`, `mocks/fake_rover.py` (speaks the full WS protocol with canned images) | **I0 (H2):** P4's brain connects to fake_rover and to the real Pi WS stub; heartbeat flows |
| H2–H6 | motors, servos, imu, camera, safety, ws_server | `drive 0.3` and `turn 90` work from a WS client; watchdog stops the motors when you kill the client mid-drive |
| H6–H10 | calibration, pose, heading (gyro), snapshot server | 360° turn error < 10° (gyro) / < 20° (timed); 1 m drive < 5 cm drift |
| H8–H14 | station, bev, proposals, local_store, uploader | **I1/I2:** a `station` produces 8 keyframes + BEV + proposals on the SD card and in the mapper's `/data/runs/…` (P3) within 60 s |
| H12–H18 | approach, scoop sequence, overhead tracker | Approach puts the pebble in the scoop zone ≥ 8/10; scoop pick ≥ 7/10 (with P1) |
| H18–H24 | VO fusion, robustness: reconnect, camera re-open, upload retry | **I3/I4:** full mission end to end with the brain; unplugging Wi-Fi for 10 s → motors stop, then the run resumes after reconnect |
| H24–H28 | Bug fixes only, tune thresholds | **I5 freeze** |

Unit tests in `rover/tests/`: `test_bev.py` (pixel→xy on a synthetic homography), `test_proposals.py` (finds 3 dark blobs on a synthetic image), `test_safety.py` (watchdog timing with a fake clock), `test_protocol.py` (every op round-trips through pydantic).

## 8. Mocks
- **You provide** `mocks/fake_rover.py`: same WS protocol and timing (station ≈ 5 s), poses moving as commanded, canned station bundles from `mocks/data/stations/` uploaded to the real mapper (or fake_mapper), and `scoop` returning a canned `present.jpg`. P4 and P3 build against it from H2.
- **You use** `mocks/fake_mapper.py` (P3) until the real mapper is up.

## 9. Watch-outs
- **Stale frames**: USB webcams buffer; flush 3 frames before a capture.
- Auto-exposure changes between views break VO and stitching → lock exposure if `cv2.CAP_PROP_AUTO_EXPOSURE` works on this camera ⚠️.
- Don't block the asyncio loop with motion or OpenCV; run ops in a worker thread and keep heartbeat/estop handling on the loop.
- **E-stop must work even while an op runs**: the estop handler sets the latch and calls `Motors.stop()` directly.
- The scoop must be in STOW before every station (or it's in the photos).
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
**Station upload:** `POST /ingest/station` multipart: `station.json` (`run_id, station_id, t, pose, keyframes[{kf_id,file,heading_deg,t}], bev{file,res_m_px,w_px,h_px,frame}, proposals[{prop_id,kf_id,bbox_px,crop,xy,size_m,contrast}]`) + `kf_*.jpg`, `bev.png`, `crop_p_*.jpg`. Reply `{ok, station_id, stored, urls{bev, crops{}}}`.
**Sample upload:** `POST /ingest/sample` multipart: `sample.json {run_id, sample_id, cand_id, t, pose}` + `present.jpg` → `{ok, url}`.
**Timeouts:** heartbeat every 0.25 s; stop if none for 1.0 s while moving; station ≤ 60 s; approach ≤ 45 s; scoop ≤ 30 s.
**Overhead:** `{"type":"overhead_pose","t","src":"overhead","x","y","heading_deg","marker_id":0,"reproj_err_px","visible"}` at 10 Hz.
