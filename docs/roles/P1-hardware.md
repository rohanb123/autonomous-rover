# P1: Hardware (pure HW)

> Read top to bottom once, then use it as a checklist. Detailed mechanics are in [01](../01-hardware-and-assembly.md); this brief tells you **what to do, when, and how you know it's done**.

## 1. Goal
A rover that drives straight-ish, turns in place, carries a rigid camera mast and a 2-servo scoop that can pick up a 1.5–3 cm pebble, show it to the camera and drop it. Plus the overhead camera stand, ArUco markers and the arena. **It must survive 20+ runs without a brownout or a loose wire.**

## 2. Scope (you own) / not yours
| Yours | Not yours |
|---|---|
| Chassis, motors, wheels, both battery packs, kill switch, star ground | Any code (P2 writes `motors.py`, `servos.py`, etc.) |
| Wiring harness per the pinout below, labels on every wire | Pin assignments in code (P2), but they **must** match this table |
| Scoop arm + bucket fabrication, servo mounting, mechanical stops | Servo µs calibration (P2 `calibrate_scoop.py`, you assist) |
| Camera mast (rigid, 30–35° down) | Camera homography (P2) |
| IMU + PCA9685 + optional ADS1115 physical mounting | Drivers (P2) |
| ArUco markers (print, mount, measure), overhead stand | ArUco tracker code (P2 `gx10/overhead/`) |
| Arena: home pad, heading arrow, ground, rocks, pickable pebbles | Arena photos for few-shot ML (P3 takes them; you lay out the arena) |
| `hardware/` folder: `bom.csv`, `wiring/harness.md`, `scoop/`, `arena/`, `checklists/preflight.md` | Everything else in the repo |
| Purchases/sourcing of the NEED items | |

## 3. Parts (your BOM, from [01 §3](../01-hardware-and-assembly.md#3-bill-of-materials))
**HAVE (kit, verify at H0):** Pi (4 or 5?), chassis + DC gearmotors (no encoders), 2 servos (model?), IMU (chip?), rotary sensor (pot or encoder?), stepper (dropped), 2 webcams.
**NEED (sourced prices, 2026-10-23):** TB6612 $6.95 (Adafruit #2448) if the kit has no driver · PCA9685 $14.95 (#815) · 2 spare SG92R $5.95 each (#169) · 2× 4×AA holder with switch $2.95 (#830) + NiMH cells · 4700 µF cap $1.95 (#1589) · 100 µF ×10 $1.95 (#2193) · MPU-6050 $12.95 (#3886) **only if the kit IMU lacks a gyro** · ADS1115 $14.95 (#1085) **only if the rotary sensor is a pot** · kill switch · travel router.
**MAKE:** arm (popsicle sticks), scoop bucket (~5×6×2.5 cm), 5 ArUco markers, arena.

## 4. Decisions you implement
- **No pan/tilt head.** The camera is fixed on the mast; the rover rotates to look around ([why](../01-hardware-and-assembly.md#look-around-decision)).
- **Stepper dropped.** Don't mount it.
- **Rotary sensor:** if it's a 3-pin pot, couple it to the shoulder axis (optional, if time) → ADS1115 A0. If it's an encoder, leave it out.
- **Second webcam = overhead camera** on a stand ≥ 1.6 m above the arena, USB to the GX10.

## 5. Pinout you wire (BCM numbering)
| Signal | BCM | Phys | To |
|---|---|---|---|
| SDA | 2 | 3 | PCA9685, IMU, ADS1115 |
| SCL | 3 | 5 | same |
| PWMA | 12 | 32 | TB6612 PWMA (left) |
| AIN1 / AIN2 | 5 / 6 | 29 / 31 | TB6612 |
| PWMB | 13 | 33 | TB6612 PWMB (right) |
| BIN1 / BIN2 | 20 / 21 | 38 / 40 | TB6612 |
| STBY | 26 | 37 | TB6612 STBY |
| LED | 22 | 15 | LED + 330 Ω |
| 3V3 | — | 1 | logic VCC of PCA9685, IMU, ADS1115, TB6612 |
| GND | — | 6 | star ground |

PCA9685: **ch0 = shoulder, ch1 = wrist**, ch2–3 spares. V+ from the servo pack with the 4700 µF cap. TB6612 VM from the motor pack with a 100 µF cap. Both packs go through the kill switch. Pi on its own USB-C bank. **All grounds meet at one terminal.**
Expected I2C: `0x40` PCA9685, `0x68` IMU (MPU-6050), `0x48` ADS1115.

## 6. Build order with checkpoints
| When | Step | ✅ Checkpoint (show P2 / P4) |
|---|---|---|
| H0–H1 | Inventory, photos, read chip markings. Fill U-1…U-8 in [05](../05-unknowns-and-risks.md#unknowns). Order/borrow NEED items | Unknowns table filled; purchase list sent to the team |
| H1–H3 | Chassis + motors + wheels; mark the rover center dot | Wheels spin free; frame square |
| H2–H4 | Packs, kill switch, star ground, caps | Multimeter: VM and V+ in range; kill switch cuts both; Pi stays up |
| H3–H5 | TB6612 harness | **I0 support:** P2 spins both wheels both ways; STBY low stops them |
| H4–H6 | I2C deck (IMU flat, centered, arrow = forward) | `i2cdetect -y 1` shows the addresses |
| H5–H7 | Mast + camera (30–35° down) | Ground visible from ~10 cm ahead of the scoop lip to ≥ 1.5 m; mast doesn't wobble when you tap it |
| H6–H12 | Scoop: center servos at 1500 µs **before** fitting horns; arm L1 ≈ 10 cm, L2 ≈ 5 cm; shoulder at the front-left corner, 4–5 cm left of the centerline | Hand-cycle STOW/DEPLOY/CURL/PRESENT/DUMP; STOW is out of the camera's view; no collisions |
| H8–H14 | Overhead stand, markers (id 0 on the rover top, ids 1–4 at the arena corners), home pad + arrow, rocks + 6–10 pebbles | Overhead sees all 5 markers; marker positions measured into `hardware/arena/markers.yaml` |
| H14–H24 | Harden: strain relief, zip ties, labels, spares pre-horned | 10 stations + 3 scoops in a row, no brownout |
| H24+ | Pre-flight checklist; battery rotation; repairs only | Checklist printed and used before every run |

## 7. Acceptance tests (yours)
1. **Power isolation:** stall a servo by hand while the Pi runs → the Pi doesn't reboot.
2. **Kill switch:** motors and servos dead, Pi alive.
3. **Scoop pick:** from DEPLOY, a 4 cm push + CURL picks up a 2 cm pebble ≥ 7 out of 10 tries (P2 runs the sequence, you tune the mechanics).
4. **Present:** in PRESENT, the pebble sits in the same image region every time and is in focus (check P2's snapshot).
5. **Torque:** the shoulder lifts the loaded scoop from DEPLOY to PRESENT without stalling, 10 times in a row.
6. **Markers:** printed side length measured to ±1 mm and recorded.

## 8. What to measure and hand over (to P2, in `hardware/`)
- Shoulder axis height above ground (H_s), L1, L2, actual lip position in DEPLOY (cm ahead of the shoulder axis and of the rover center).
- Camera mast height and tilt angle.
- Wheel diameter and track width (rover center to wheel centers).
- Marker side lengths; corner marker positions in the mission frame (origin = home pad cross, +x = arrow).
- Battery type and voltage at full charge for each pack.

## 9. Mocks / working without others
You don't depend on software to build. For scoop testing before P2's code exists, use a servo tester or a 10-line script P2 gives you at I0 (`python -m rover.servos --ch 0 --us 1500`).

## 10. Watch-outs
- **Never power servos from the Pi's 5 V pin.**
- Center servos before attaching horns or the poses will be out of range.
- A wobbly mast silently breaks mapping; glue + a diagonal brace.
- Glare on markers: matte paper, no lamination.
- Fresh alkaline 4×AA ≈ 6+ V; check that the servos tolerate it, or use NiMH (4.8 V nominal).
- Keep the IMU away from the motors and at the turning center.
- Bring spare servos pre-mounted on horns: a stripped servo is a 2-minute swap, not 30.

## 11. Contracts you need (copy of doc 00 / 01)
- **Frames:** mission origin = home pad cross; +x = arrow direction; +y = left; heading CCW+ in degrees. Rover origin = the center dot you mark.
- **Pinout and I2C:** §5 above is a copy of [01 §4](../01-hardware-and-assembly.md#4-wiring-and-pinout-bcm-numbering).
- **Scoop poses:** STOW, DEPLOY, CURL, PRESENT, DUMP ([01 §6.2](../01-hardware-and-assembly.md#62-poses)).
- **Visit exclusion radius:** 0.20 m. Space pebbles ≥ 0.25 m apart if you want each to be sampleable.
- **Markers:** ArUco `DICT_4X4_50`; id 0 = rover (top, ~8 cm); ids 1–4 = arena corners (~12 cm).
