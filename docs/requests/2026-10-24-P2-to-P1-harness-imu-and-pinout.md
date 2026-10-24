# P2 → P1: the IMU chip, the as-built pinout, and motor direction

**From:** Shrujan (P2, rover runtime) · **To:** Aryan (P1, hardware) · **Date:** 2026-10-24
**Status:** open

The I1 rover runtime is written and unit-tested (`rover/rover/{config,motors,servos,camera,pose,motion,safety,ws_server,main}.py`).
It drives `drive`/`turn` from a WS client today with the pin writes recorded instead of
energised, so nothing below blocks me from writing code — but every one of them blocks the
numbers being *true*, and (a) has a purchase lead time.

In rough order of how much they hurt:

## (a) U-2: what IMU is in the kit? — the one with a deadline

Please read the chip marking and send me `i2cdetect -y 1`.

This decides whether turns are closed-loop at all. `rover/rover/imu.py` will return
`"mpu6050@0x68"`, `"adxl345@0x53"` or `"none"`, and `Motion.turn()` already branches on
`has_gyro()`: with a gyro it integrates `gz_dps`, without one it falls back to a timed turn
off `calib.deg_per_s_at`.

Why it is urgent rather than merely important: docs/05 **R2 (heading drift) is our
highest-scoring risk at 9**, its mitigation is gyro-closed turns, and its trigger is "360°
error > 20° at I1 → buy/borrow MPU-6050 now". An MPU-6050 has a shipping lead time, so if the
kit IMU is an accel-only ADXL345 we want to know before I1, not at it.

A timed turn is a real fallback (it is tested), but doc 05 also says a 5° heading error at
1.5 m is 13 cm of target error, which is larger than the 0.20 m exclusion radius. Open-loop
turns will cost us accuracy at every station.

## (b) U-1: confirm Pi 4 vs Pi 5

I have been told Pi 5 and have coded for it: `gpiozero` with the `lgpio` pin factory, since
pigpio has no Pi 5 support ([pigpio#589](https://github.com/joan2937/pigpio/issues/589)).
That combination works on both boards, so this is a confirmation rather than a blocker —
please just tick it off in docs/05 so it stops being an open unknown.

## (c) Does the as-built wiring match docs/01 §4?

`rover/calib/calib.yaml` carries the pinout and **`rover/tests/test_config.py` asserts it
equals docs/01 §4 exactly**:

```
pwm_a 12  ain1 5  ain2 6  pwm_b 13  bin1 20  bin2 21  stby 26  led 22
PCA9685 0x40   ADS1115 0x48   IMU 0x68 then 0x53
```

That test is deliberate: if the harness ends up on different pins and doc 01 is not updated,
my build goes red instead of the rover silently driving the wrong pins. So if you move a pin,
please write it into docs/01 §4 and `hardware/wiring/harness.md` and send me a request — I
will update calib.yaml rather than guess.

## (d) Is either motor wired backwards?

docs/01 §4 says to fix a backwards motor by swapping its two wires, and I would rather you did
that than compensate in software. But `calib.yaml` has `motion.invert_left` /
`motion.invert_right` as an escape hatch if the harness is already glued down — just tell me
which side, and whether "left" means the left wheel looking *forward* from the rover's point
of view (that is what I have assumed).

Related: `motor_trim` is currently `0.0`. Positive trim scales the **left** wheel by
`(1 - trim)`. `calibrate_motion.py` (I2) will measure it.

## (e) Smaller measurements, by I1

- **U-5**: the servo model and whether the shoulder lifts a loaded scoop. `calib.yaml` clamps
  µs to `[600, 2400]`, which is a guess — narrow it once you know the real mechanical stops,
  because a servo driven past its stop stalls and cooks.
- **U-6**: measured deck/shoulder height. It sets `scoop.scoop_zone_px`, currently a guess.
- **U-7**: webcam minimum focus distance (photograph a ruler at 10/15/20/30 cm). It decides
  whether the PRESENT pose is even in focus, and sets `scoop.present_roi_px`.

## (f) Ping me at CP3 and CP4

Those are my two unblocking checkpoints: the drive harness, and `i2cdetect` showing the bus.
The moment either passes I can run `calibrate_motion.py` and replace the placeholder numbers.

## What is placeholder right now, so nobody trusts it

`calib.yaml` has `calibrated_at: null` and the runtime logs a warning at startup saying every
motion number is a placeholder. `m_per_s_at`, `deg_per_s_at` and `motor_trim` are invented but
plausible; the BEV `homography` is the identity matrix, which is geometrically meaningless, so
`station.py` (I2) will refuse to run until a real calibration exists rather than upload
nonsense to P3's mapper.
