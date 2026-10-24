# 05: Unknowns and risk register

**Owner: P4** (each row names who resolves it). Update the "Status" column as things get resolved; don't delete rows.

## Unknowns
Everything here is **⚠️ UNVERIFIED** until its status says resolved. "Default" is what we assume and build for until then.

| ID | Unknown | Why it matters | Default until resolved | How to resolve | Who | By |
|---|---|---|---|---|---|---|
| U-1 | Kit Pi: **Pi 4 or Pi 5?** | GPIO library (Pi 5: no pigpio; use `lgpio`), power budget (Pi 5 supply requirement ⚠️ check the official PSU spec) | Code for `gpiozero` + `lgpio` (works on both) | Read the board silkscreen | P1 | by I0 |
| U-2 | Kit IMU chip (gyro or accel only?) | Closed-loop turns and heading quality | Buy/borrow MPU-6050 (0x68) if no gyro | Read the chip marking; `i2cdetect` | P1 → P2 | by I0 |
| U-3 | Stepper model | — | **Dropped** (no use without a head; [01 §1](01-hardware-and-assembly.md#look-around-decision)) | None needed | — | closed |
| U-4 | Rotary sensor type: pot or encoder? | Pot → optional shoulder feedback via ADS1115; encoder → unused | Leave out | 3 pins + resistance across the outer pins = pot | P1 | by I0 |
| U-5 | Kit servo model / torque | Can the shoulder lift a loaded scoop? | Spare SG92R ($5.95, Adafruit #169) | Read the label; torque test (01 §6) | P1 | by I1 |
| U-6 | Chassis deck height / shoulder height | Scoop geometry (lip reach ≈ 12.0 cm in the worked example) | Worked example in 01 §6 | Measure | P1 | by I0 |
| U-7 | Webcam minimum focus distance | PRESENT pose must be in focus | ≈ 15–20 cm assumed | Photograph a ruler at 10/15/20/30 cm | P1 + P2 | by I1 |
| U-8 | Kit battery holder (cells, voltage) | Motor/servo supply split | Buy 2 × 4×AA holders (#830) | Inspect | P1 | by I0 |
| U-9 | Venue network (can we bring a router? is there internet?) | Everything is on one LAN; datasets and model weights need internet once | Own travel router, no internet at runtime; pre-download | Ask organizers | P4 | before the event |
| U-10 | SATS-JSON encoding of byte arrays over HTTP | Would let us store tiles as bytes | **Avoided**: base64 strings | Not needed | P4 | closed (design) |
| U-11 | ✅ **Resolved 2026-10-24** (local v2.13.0, `test_live_insert_pose_round_trip`): positional arrays map in declaration order. HTTP reducer call: positional JSON array matches arg order as declared | `StdbClient.call` depends on it | Positional array in declaration order | I0 test (02 §12.2) | P4 | **I0** |
| U-12 | ✅ **Resolved 2026-10-24** (local v2.13.0): yes, `ctx.sender.toHexString()` works; `claim_writer`/`add_writer` use it. Does server-side `Identity` have `toHexString()` in the TS module? | `writer` table key | Fallback: compare with `isEqual` over a stored identity column | Compile a 3-line reducer | P4 | by I1 |
| U-13 | SuperCam observation log has an AEGIS flag? | Stretch: Perseverance priors | ChemCam (MSL) priors only | Open the PDS log | P3 | stretch |
| U-14 | Terms of the mars.nasa.gov raw-image JSON feeds (unofficial) | Optional backdrop images | Don't depend on them; attribute NASA/JPL-Caltech if used | Read the site terms | P3 | optional |
| U-15 | `open_clip_torch` + Ollama caption model (P3), `faster-whisper` + Ollama NLU model (P4) run on GX10 ARM64 + CUDA | Core vision + voice | CPU fallbacks; text-only missions | Early smoke tests | P3, P4 | by I1 |
| U-16 | chrony available on DGX OS | Clock sync for replay ordering | `timedatectl` / `systemd-timesyncd` to the GX10 | `apt policy chrony` | P4 | by I0 |
| U-17 | **SpaceXSI / SpacetimeDB track rules** (what counts, judging criteria) | Scope priorities | Known only from the team brief: we use SpacetimeDB as the system of record and space data for training | Read the official track page with the organizers | P4 | before the event |
| U-18 | Exact Kaggle slug for the AI4Mars mirror | Backup download only | Use the HF mirror | Search Kaggle | P3 | by I0 |
| U-19 | AI4Mars license: Zenodo says CC-BY-4.0; NASA portal says "not specified" | Attribution | Treat as CC-BY-4.0, attribute | — | P3 | doc only |
| U-20 | Pi OpenCV build includes `cv2.aruco` | Only the GX10 needs ArUco (overhead) | Overhead runs on the GX10 | `python -c "import cv2; cv2.aruco"` | P2 | by I0 |
| U-21 | Webcam supports manual exposure via OpenCV | Stable stitching/VO | Auto exposure, settle 0.3 s | Try `CAP_PROP_AUTO_EXPOSURE` | P2 | by I1 |
| U-22 | TB6612 current rating vs. kit motor stall current | Driver overheating | Use the Adafruit breakout; measure stall current | Datasheet + multimeter | P1 | by I1 |
| U-23 | PCA9685 Python library on Pi 5 (Blinka) vs plain `smbus2` | Servo driver | `smbus2` direct register writes as fallback | Install test | P2 | by I1 |

## Risk register
L = likelihood, I = impact (1–3). Score = L×I.

| # | Risk | L | I | Score | Mitigation | Trigger → action | Owner |
|---|---|---|---|---|---|---|---|
| R1 | **P3 overload** (ML + vision + mapper + map view + assay; voice moved to P4, arena photos to P1) | 3 | 3 | 9 | Mocks by I0; **cut list in order:** (1) VLM captions → template strings, (2) posegraph → plain DR mosaic, (3) voice → text missions only (P4; keep quick commands as buttons), (4) arena few-shot → MSL-only heads, (5) Chart error bars. **Never cut:** `/score`, mapper ingest, MapView, Bradford sim + panel | P3 behind at I2 → apply cuts 1–2; if still behind, P4 takes over `dashboard/src/assay/` as a whole directory (never split a service between owners) | P3, P4 |
| R2 | **Heading drift** breaks target positions and the map | 3 | 3 | 9 | Gyro-closed turns, BEV VO yaw, overhead ArUco `set_pose` at every station, short drives | 360° error > 20° at I1 → buy/borrow MPU-6050 now | P2 |
| R3 | Scoop fails to pick pebbles | 2 | 3 | 6 | Shallow bucket with a lip, pebbles 1.5–3 cm on loose substrate, retry once, `empty_scoop` outcome is still logged | < 5/10 before I3 → bigger bucket, slower push, more DEPLOY angle | P1, P2 |
| R4 | Brownouts / servo resets the Pi | 2 | 3 | 6 | Separate packs, 4700 µF cap, star ground, Pi on its own bank | Any Pi reboot under load → stop and fix power first | P1 |
| R5 | Wi-Fi dropouts at the venue | 2 | 3 | 6 | Own router; heartbeat watchdog stops motors; Pi local store + upload retry | > 1 drop / run → move router closer, 5 GHz | P4, P2 |
| R6 | **Assay misread as real science** | 2 | 3 | 6 | "SIMULATED — proof of concept" in panel, chart title, drawer chip; reducer rejects `simulated=false`; README states it; never say "detected protein" | Any reviewer confusion → bigger banner | P3 |
| R7 | AI4Mars download too large/slow | 2 | 2 | 4 | HF mirror subset (6.47 GB), start first thing or pre-download; MSL v2.1 (55 MB) alone suffices for core | Not done by I1 → drop the terrain head | P3 |
| R8 | ARM64 library incompatibility (open_clip, faster-whisper, Ollama) | 2 | 2 | 4 | Early smoke tests; CPU fallbacks | Fails before I1 → CPU path / text-only | P3, P4 |
| R9 | STDB API surprises (positional args, BigInt, bindings drift) | 2 | 2 | 4 | I0 test; regenerate bindings in `make up`; pinned v2.13.0 | I0 fails → Maincloud or named-arg fallback per docs | P4 |
| R10 | Domain gap (Mars data vs arena) → bad rankings | 3 | 2 | 6 | Arena few-shot (×5 weight); priors still apply; novelty is domain-free | Arena accuracy < 50% → increase novelty weight | P3 |
| R11 | Dashboard bandwidth from base64 tiles/keyframes | 1 | 2 | 2 | Size caps in reducers; per-run subscriptions; `keyframe_meta` view (stretch) | UI lag > 1 s → drop thumbs from live | P4 |
| R12 | Contract drift between teams | 2 | 3 | 6 | `shared/py` pydantic for every message; change process in AGENTS.md; role-file copies | Any mismatch at a gate → fix the code to 00, not 00 to the code | P4 |
| R13 | Clock skew → replay ordering wrong | 1 | 2 | 2 | chrony/timesyncd to the GX10 (U-16) | Skew > 50 ms → resync | P4 |
| R14 | E-STOP doesn't stop in time | 1 | 3 | 3 | Bypasses the FSM queue; Pi latch calls `Motors.stop()` directly; physical kill switch | Test at every gate | P4, P2, P1 |
