#!/usr/bin/env python3
"""Spin up to 360 degrees reading sonar on every tick; stop and approach on first detection.

The rover turns continuously (no step pauses). The sonar is polled every 5 ms via gpiozero's
non-blocking .distance property. The first reading that drops below DETECT_RATIO × baseline
immediately halts the spin and drives the rover straight toward the object, stopping at
APPROACH_STOP_M.

Uses the MPU6050 gyro (z axis) to measure the turn. Falls back to timed turns without an IMU.

Wiring (see the rover wiring diagrams):
  Left motor  (L293D pins 3/6):   EN1,2 -> GPIO12, 1A -> GPIO5,  2A -> GPIO6
  Right motor (L293D pins 11/14): EN3,4 -> GPIO13, 3A -> GPIO20, 4A -> GPIO21
  MPU6050: SDA -> GPIO2, SCL -> GPIO3, VCC -> 3V3, address 0x68 (I2C enabled)
  HC-SR04:  TRIG -> GPIO23, ECHO -> GPIO24, VCC -> 5V

Keep the rover still for the first 2 s (gyro calibration). Ctrl+C stops the motors.
"""

import time

from gpiozero import Motor

# ---- motor / turn settings ----
TURN_SPEED = 0.35       # 0..1; spin duty cycle
APPROACH_SPEED = 0.50   # 0..1; forward drive duty cycle
TOTAL_DEG = 360.0
STOP_EARLY_DEG = 3.0    # cut spin power this many degrees early to allow coasting
TURN_TIMEOUT_S = 30.0   # safety abort if full 360° takes longer than this
APPROACH_TIMEOUT_S = 10.0  # abort approach if object never gets close enough
APPROACH_STOP_M = 0.15  # stop driving when the sonar reads this close
TURN_LEFT = True        # True = counter-clockwise seen from above
FLIP_LEFT = False       # set True if left motor is wired backwards
FLIP_RIGHT = False      # set True if right motor is wired backwards
TIMED_DEG_PER_S = 90.0  # only used without the IMU; measure and adjust

# ---- sonar settings ----
SONAR_TRIG = 23
SONAR_ECHO = 24
SONAR_MAX_DIST_M = 5.0   # HC-SR04 hardware max ~4 m; 5 m just extends the echo timeout
DETECT_RATIO = 0.75      # flag if live reading < baseline × this (25%+ closer than open space)

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
    sonar = DistanceSensor(echo=SONAR_ECHO, trigger=SONAR_TRIG, max_distance=SONAR_MAX_DIST_M)
    print("Sonar ready.")
except Exception as e:
    print(f"Sonar not available ({e}); distance detection disabled.")


# ---- helpers ----------------------------------------------------------------

def gz_dps():
    hi, lo = bus.read_i2c_block_data(MPU, 0x47, 2)
    v = (hi << 8) | lo
    if v > 32767:
        v -= 65536
    return v / 131.0


def _wheel(motor, forward, flipped):
    if forward != flipped:
        motor.forward(TURN_SPEED)
    else:
        motor.backward(TURN_SPEED)


def start_turn():
    # CCW: left wheel backward, right wheel forward (or flipped if wiring is reversed).
    _wheel(left, not TURN_LEFT, FLIP_LEFT)
    _wheel(right, TURN_LEFT, FLIP_RIGHT)


def drive_fwd():
    """Drive straight forward, respecting motor wiring flips."""
    if not FLIP_LEFT:
        left.forward(APPROACH_SPEED)
    else:
        left.backward(APPROACH_SPEED)
    if not FLIP_RIGHT:
        right.forward(APPROACH_SPEED)
    else:
        right.backward(APPROACH_SPEED)


def halt():
    left.stop()
    right.stop()


def sonar_dist():
    """Non-blocking: return the sensor's latest distance in metres, or None."""
    if sonar is None:
        return None
    return sonar.distance  # gpiozero updates this in a background thread


def object_detected(dist, baseline):
    return dist is not None and baseline is not None and dist < baseline * DETECT_RATIO


class Heading:
    """Integrates gz_dps continuously. Call update() every loop tick."""

    def __init__(self):
        self.bias = 0.0
        self.deg = 0.0
        self.last = time.perf_counter()

    def calibrate(self, seconds=2.0):
        print(f"Calibrating gyro for {seconds:.0f} s — keep the rover still...")
        samples, t0 = [], time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            samples.append(gz_dps())
            time.sleep(0.005)
        self.bias = sum(samples) / len(samples)
        self.last = time.perf_counter()
        print(f"Gyro bias {self.bias:+.2f} deg/s")

    def update(self):
        now = time.perf_counter()
        rate = abs(gz_dps() - self.bias)
        if rate > 0.5:  # 0.5 deg/s deadband: ignore sensor noise while stationary
            self.deg += rate * (now - self.last)
        self.last = now
        return self.deg


# ---- main -------------------------------------------------------------------

try:
    # Baseline: read sonar once while the rover is still. gpiozero fires trigger pulses
    # in the background, so .distance is already updated by the time calibration finishes.
    baseline_dist = sonar_dist()
    if baseline_dist is not None:
        print(f"Baseline distance: {baseline_dist:.3f} m")

    detected = False
    detect_dist = None

    if bus:
        h = Heading()
        h.calibrate()  # 2 s; sonar keeps updating during this time

        # Re-read baseline after calibration (rover is still, sonar has had time to settle).
        baseline_dist = sonar_dist()
        if baseline_dist is not None:
            print(f"Baseline (post-cal): {baseline_dist:.3f} m")

        start_turn()
        t0 = time.perf_counter()

        while h.update() < TOTAL_DEG - STOP_EARLY_DEG:
            if time.perf_counter() - t0 > TURN_TIMEOUT_S:
                halt()
                raise RuntimeError("spin timed out; wheels stalled or speed too low")

            dist = sonar_dist()
            if object_detected(dist, baseline_dist):
                halt()
                detected = True
                detect_dist = dist
                print(f"Object at {dist:.3f} m after {h.deg:.1f}° — stopping spin")
                break

            time.sleep(0.005)
        else:
            halt()
            print(f"Completed {h.deg:.1f}° scan — no object detected.")

        scan_deg = h.deg

    else:
        # Timed fallback: wall-clock spin, same continuous sonar logic.
        total_s = TOTAL_DEG / TIMED_DEG_PER_S
        start_turn()
        t0 = time.perf_counter()

        while True:
            elapsed = time.perf_counter() - t0
            if elapsed >= total_s:
                halt()
                print(f"Completed 360° scan (timed) — no object detected.")
                break

            dist = sonar_dist()
            if object_detected(dist, baseline_dist):
                halt()
                detected = True
                detect_dist = dist
                print(f"Object at {dist:.3f} m after ~{elapsed * TIMED_DEG_PER_S:.0f}° (timed) — stopping spin")
                break

            time.sleep(0.005)

        scan_deg = None  # not measured in timed mode

    # ---- approach -----------------------------------------------------------
    if detected:
        print(f"Approaching (stop at {APPROACH_STOP_M:.2f} m)...")
        drive_fwd()
        t_ap = time.perf_counter()

        while True:
            d = sonar_dist()
            if d is not None and d <= APPROACH_STOP_M:
                halt()
                print(f"Reached object at {d:.3f} m.")
                break
            if time.perf_counter() - t_ap > APPROACH_TIMEOUT_S:
                halt()
                print("Approach timed out.")
                break
            time.sleep(0.005)

    if scan_deg is not None:
        print(f"Done. Scan covered {scan_deg:.1f} deg.")
    else:
        print("Done.")

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
