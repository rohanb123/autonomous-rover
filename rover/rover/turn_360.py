#!/usr/bin/env python3
"""Spin the rover 360 degrees in place, pausing 1 s after every 45 degrees.

Uses the MPU6050 gyro (z axis) to measure the turn, the same idea as the station spin in the
P2 role doc. If the IMU isn't found, it falls back to timed turns (much less accurate).

The HC-SR04 sonar reads distance at every stop. The step with the shortest reading (if it
drops below DETECT_RATIO × baseline) is flagged as the object direction. After the full 360°
the rover turns that many 45° steps to face the object and stops.

Wiring (see the rover wiring diagrams):
  Left motor  (L293D pins 3/6):   EN1,2 -> GPIO12, 1A -> GPIO5,  2A -> GPIO6
  Right motor (L293D pins 11/14): EN3,4 -> GPIO13, 3A -> GPIO20, 4A -> GPIO21
  MPU6050: SDA -> GPIO2, SCL -> GPIO3, VCC -> 3V3, address 0x68 (I2C enabled)
  HC-SR04:  TRIG -> GPIO23, ECHO -> GPIO24, VCC -> 5V

Keep the rover still for the first 2 s (gyro calibration). Ctrl+C stops the motors.
"""

import time

from gpiozero import Motor

# ---- settings ----
TURN_SPEED = 0.65  # 0..1; full power on a 9V battery (it sags under load, motors see ~5-6V)
STEP_DEG = 45.0
TOTAL_DEG = 360.0
PAUSE_S = 1.0
STOP_EARLY_DEG = 3.0  # cut power this early to allow for coasting; tune after a test run
STEP_TIMEOUT_S = 8.0  # stop if a 45 degree step takes longer than this (stalled wheels/flat battery)
TURN_LEFT = True  # True = counter-clockwise seen from above
FLIP_LEFT = False  # same meaning as in motor_obstacle_flipped.py
FLIP_RIGHT = False
TIMED_DEG_PER_S = 90.0  # only used without the IMU; measure and adjust

# ---- sonar settings ----
SONAR_TRIG = 23
SONAR_ECHO = 24
SONAR_SAMPLES = 5    # readings averaged at each stop (one every 50 ms = 250 ms total)
DETECT_RATIO = 0.75  # flag if distance < baseline × this (object is at least 25% closer)

left = Motor(forward=6, backward=5, enable=12)
right = Motor(forward=20, backward=21, enable=13)

# ---- gyro ----
MPU = 0x68
bus = None
try:
    try:
        from smbus2 import SMBus
    except ImportError:
        from smbus import SMBus
    bus = SMBus(1)
    bus.write_byte_data(MPU, 0x6B, 0x00)  # wake up
    bus.write_byte_data(MPU, 0x1A, 0x03)  # digital low-pass filter ~44 Hz (less noise)
    bus.write_byte_data(MPU, 0x1B, 0x00)  # gyro range +/-250 deg/s
    time.sleep(0.1)
except Exception as e:
    bus = None
    print(f"IMU not available ({e}); using timed turns instead.")

# ---- sonar ----
sonar = None
try:
    from gpiozero import DistanceSensor
    sonar = DistanceSensor(echo=SONAR_ECHO, trigger=SONAR_TRIG, max_distance=4.0)
    print("Sonar ready.")
except Exception as e:
    print(f"Sonar not available ({e}); distance detection disabled.")


def gz_dps():
    hi, lo = bus.read_i2c_block_data(MPU, 0x47, 2)
    v = (hi << 8) | lo
    if v > 32767:
        v -= 65536
    return v / 131.0


def wheel(motor, forward, flipped):
    if forward != flipped:
        motor.forward(TURN_SPEED)
    else:
        motor.backward(TURN_SPEED)


def start_turn():
    # Turning left (CCW): left wheel backward, right wheel forward.
    wheel(left, not TURN_LEFT, FLIP_LEFT)
    wheel(right, TURN_LEFT, FLIP_RIGHT)


def halt():
    left.stop()
    right.stop()


def read_dist_m():
    """Average SONAR_SAMPLES sonar readings. Returns metres, or None if sonar unavailable."""
    if sonar is None:
        return None
    readings = []
    for _ in range(SONAR_SAMPLES):
        d = sonar.distance  # gpiozero returns 0..max_distance in metres
        if d is not None:
            readings.append(d)
        time.sleep(0.05)
    return sum(readings) / len(readings) if readings else None


class Heading:
    """Integrates the gyro continuously, including during pauses, so coasting is counted."""

    def __init__(self):
        self.bias = 0.0
        self.deg = 0.0
        self.last = time.perf_counter()

    def calibrate(self, seconds=2.0):
        print(f"Calibrating gyro for {seconds:.0f} s, keep the rover still...")
        samples, t0 = [], time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            samples.append(gz_dps())
            time.sleep(0.005)
        self.bias = sum(samples) / len(samples)
        self.last = time.perf_counter()
        print(f"Gyro bias {self.bias:+.2f} deg/s")

    def update(self):
        now = time.perf_counter()
        # abs() so the result doesn't depend on which way up the IMU is mounted;
        # the 0.5 deg/s deadband stops sensor noise from adding up while standing still.
        rate = abs(gz_dps() - self.bias)
        if rate > 0.5:
            self.deg += rate * (now - self.last)
        self.last = now
        return self.deg

    def wait(self, seconds):
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            self.update()
            time.sleep(0.005)


def _check_dist(k, dist, baseline_dist, flagged_step, min_dist):
    """Update (flagged_step, min_dist) if this step's distance beats the current minimum."""
    if dist is not None and baseline_dist is not None and dist < baseline_dist * DETECT_RATIO:
        if dist < min_dist:
            return k, dist
    return flagged_step, min_dist


steps = int(TOTAL_DEG / STEP_DEG)
flagged_step = None
min_dist = float("inf")

try:
    # Take a baseline reading before the scan. Open-space distance the rover sees at rest.
    baseline_dist = read_dist_m()
    if baseline_dist is not None:
        print(f"Baseline distance: {baseline_dist:.3f} m")

    if bus:
        h = Heading()
        h.calibrate()
        for k in range(1, steps + 1):
            target = k * STEP_DEG  # cumulative target, so small errors don't add up
            start_turn()
            t0 = time.perf_counter()
            while h.update() < target - STOP_EARLY_DEG:
                if time.perf_counter() - t0 > STEP_TIMEOUT_S:
                    halt()
                    raise RuntimeError(
                        f"step {k} timed out at {h.deg:.0f} deg; wheels stalled or speed too low"
                    )
                time.sleep(0.005)
            halt()
            h.wait(PAUSE_S)  # keeps integrating, so coasting after the stop is measured

            dist = read_dist_m()
            flagged_step, min_dist = _check_dist(k, dist, baseline_dist, flagged_step, min_dist)
            dist_str = f"{dist:.3f} m" if dist is not None else "n/a"
            flag_str = "  *** OBJECT ***" if flagged_step == k else ""
            print(f"step {k}/{steps}: target {target:5.0f}, measured {h.deg:6.1f} deg  dist={dist_str}{flag_str}")

        scan_deg = h.deg  # snapshot before the return turn

        if flagged_step is not None:
            return_deg = flagged_step * STEP_DEG
            print(f"\nObject at step {flagged_step} ({return_deg:.0f}° from start, dist={min_dist:.3f} m).")
            print(f"Turning {flagged_step} step(s) × {STEP_DEG:.0f}° = {return_deg:.0f}° to face it...")
            return_target = h.deg + return_deg
            start_turn()
            t0 = time.perf_counter()
            while h.update() < return_target - STOP_EARLY_DEG:
                if time.perf_counter() - t0 > STEP_TIMEOUT_S * flagged_step:
                    halt()
                    raise RuntimeError("return turn timed out")
                time.sleep(0.005)
            halt()
            print(f"Stopped. Facing step {flagged_step} direction.")
        else:
            print("No object detected during scan.")

        print(f"Done. Scan: {scan_deg:.1f} deg (error {scan_deg - TOTAL_DEG:+.1f})")
    else:
        step_time = STEP_DEG / TIMED_DEG_PER_S
        for k in range(1, steps + 1):
            start_turn()
            time.sleep(step_time)
            halt()
            time.sleep(PAUSE_S)

            dist = read_dist_m()
            flagged_step, min_dist = _check_dist(k, dist, baseline_dist, flagged_step, min_dist)
            dist_str = f"{dist:.3f} m" if dist is not None else "n/a"
            flag_str = "  *** OBJECT ***" if flagged_step == k else ""
            print(f"step {k}/{steps}: timed {STEP_DEG:.0f} deg  dist={dist_str}{flag_str}")

        if flagged_step is not None:
            return_deg = flagged_step * STEP_DEG
            print(f"\nObject at step {flagged_step} ({return_deg:.0f}° from start, dist={min_dist:.3f} m).")
            print(f"Turning {flagged_step} step(s) × {STEP_DEG:.0f}° = {return_deg:.0f}° to face it...")
            start_turn()
            time.sleep(step_time * flagged_step)
            halt()
            print(f"Stopped. Facing step {flagged_step} direction.")
        else:
            print("No object detected during scan.")

        print("Done (timed, unmeasured).")

except KeyboardInterrupt:
    print("\nStopped.")
except RuntimeError as e:
    print(f"\nAborted: {e}")
finally:
    halt()
    left.close()
    right.close()
    if bus:
        bus.close()
    if sonar:
        sonar.close()
