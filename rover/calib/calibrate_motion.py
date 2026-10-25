"""Measure how far and how fast the rover actually moves, and write it into calib.yaml.

Until this has run, `m_per_s_at` and `deg_per_s_at` are invented. `Motion.drive` computes
`total_s = dist_m / interp(m_per_s_at, speed)`, so if the real speed is double the table a
commanded 1 m drive travels 2 m -- in a 3 m arena that is a wall. This is the most
consequential uncalibrated number in the repo.

It drives the motors directly rather than through `Motion`, on purpose: `Motion` consumes
the very tables being measured, so going through it would be circular.

    python rover/calib/calibrate_motion.py --straight          # the speed table
    python rover/calib/calibrate_motion.py --turns             # the turn table (gyro if present)
    python rover/calib/calibrate_motion.py --trim              # straight-line trim
    python rover/calib/calibrate_motion.py --all --dry-run     # everything, write nothing

**Put the rover on the floor with room ahead of it and keep a hand on the kill switch.** Every
leg runs for `--secs` of wall clock at a fixed duty and then stops; nothing here knows how to
avoid an obstacle, and `--secs 2` at full duty is a couple of metres.

Turns prefer the gyro: with an MPU-6050 present the degrees are integrated from the sensor, so
you are not eyeballing a protractor. Without one it asks you to measure.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "shared" / "py", _ROOT / "rover"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from rover.config import load_calib  # noqa: E402
from rover.motors import (  # noqa: E402
    GpioZeroPins,
    Motors,
    MotorsUnavailable,
    RecordingPins,
    motor_pins,
)

_spec = importlib.util.spec_from_file_location(
    "calib_edit", Path(__file__).parent / "calib_edit.py"
)
assert _spec and _spec.loader
calib_edit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calib_edit)

CALIB_PATH = _ROOT / "rover" / "calib" / "calib.yaml"

#: The duties to measure. These become the table's keys, so they must stay >= `min_speed`:
#: `interp` clamps below its first key, and a station turns at exactly `min_speed`.
DUTIES = (0.3, 0.5, 0.7, 1.0)

#: Wall-clock seconds per leg. 2 s at full duty is roughly a metre on a yellow TT gearmotor.
DEFAULT_SECS = 2.0

#: Sanity band for a 3-6 V gearmotor on a 7.2 V pack through an L293D's ~1.8 V drop.
PLAUSIBLE_M_PER_S = (0.02, 1.5)
PLAUSIBLE_DEG_PER_S = (5.0, 720.0)


class CalibrationError(RuntimeError):
    """A measurement that should not be written."""


# --- the maths (no hardware, so it is all testable) ---------------------------------------


def speed_from_run(dist_m: float, elapsed_s: float) -> float:
    """Metres per second from a measured leg."""
    if elapsed_s <= 0:
        raise CalibrationError(f"elapsed must be positive, got {elapsed_s}")
    if dist_m <= 0:
        raise CalibrationError(
            "distance must be positive; if the rover went backwards, fix "
            "invert_left/invert_right in calib.yaml rather than negating this"
        )
    value = dist_m / elapsed_s
    lo, hi = PLAUSIBLE_M_PER_S
    if not lo <= value <= hi:
        raise CalibrationError(
            f"{value:.3f} m/s is outside the plausible {lo}-{hi} m/s. Check the units "
            "(metres, not centimetres) and that the leg ran for the time it reports"
        )
    return value


def rate_from_turn(deg: float, elapsed_s: float) -> float:
    """Degrees per second from a measured spin. `deg` is the magnitude turned."""
    if elapsed_s <= 0:
        raise CalibrationError(f"elapsed must be positive, got {elapsed_s}")
    value = abs(deg) / elapsed_s
    lo, hi = PLAUSIBLE_DEG_PER_S
    if not lo <= value <= hi:
        raise CalibrationError(f"{value:.1f} deg/s is outside the plausible {lo}-{hi} deg/s")
    return value


def trim_from_drift(drift_m: float, dist_m: float, track_m: float) -> float:
    """The `motor_trim` that straightens a drifting drive.

    `drift_m` is the signed lateral offset at the end of the leg, **positive to the rover's
    LEFT** (which is +y in the mission frame, doc 00 section 4).

    For a differential drive over distance L in time t: the heading change is
    `phi = (vR - vL) * t / track`, and for a small arc the lateral offset is `d = L * phi / 2`.
    Eliminating phi and dividing by the mean speed `L / t`:

        (vR - vL) / v_mean = 2 * d * track / L**2

    A positive drift means the right wheel is running fast, and `Motors._trimmed` reads
    `trim < 0` as "slow the right wheel" -- hence the sign flip. `trim > 0` slows the left.
    """
    if dist_m <= 0 or track_m <= 0:
        raise CalibrationError("distance and track must be positive")
    trim = -2.0 * drift_m * track_m / (dist_m**2)
    if abs(trim) > 0.5:
        raise CalibrationError(
            f"a trim of {trim:+.2f} is too large to be wheel mismatch ({drift_m:+.3f} m of "
            f"drift over {dist_m:g} m). Check for a dragging wheel, a loose coupler, or a "
            "motor wired backwards (invert_left/invert_right)"
        )
    return trim


def merge_table(existing: dict[float, float], measured: dict[float, float]) -> dict[float, float]:
    """Measurements win; untouched keys survive, so a partial run is still progress."""
    return {**existing, **measured}


# --- the rig ------------------------------------------------------------------------------


class Rig:
    """Motors plus an optional gyro. Direct pin control, no Motion and no Safety.

    There is no watchdog here and no tilt cutoff -- this is a bench tool you stand over with
    the kill switch, and pretending otherwise would be worse than saying so.
    """

    def __init__(self, calib, sim: bool = False) -> None:
        self.calib = calib
        if sim:
            self.motors = Motors(calib.pins, calib.motor_trim, RecordingPins())
        else:
            pwm, out = motor_pins(calib.pins)
            try:
                self.motors = Motors(calib.pins, calib.motor_trim, GpioZeroPins(pwm, out))
            except MotorsUnavailable as e:
                raise CalibrationError(
                    f"no GPIO ({e}). Run on the Pi, or pass --sim to exercise the arithmetic"
                ) from e
        self.imu = None
        if not sim:
            with contextlib.suppress(Exception):
                from rover.imu import Imu
                from rover.i2c import Smbus2Bus

                imu = Imu(Smbus2Bus(calib.i2c.bus), tuple(calib.i2c.imu_addrs))
                self.imu = imu if imu.present and imu.has_gyro() else None

    def run_leg(self, left: float, right: float, secs: float) -> tuple[float, float]:
        """Drive for `secs`, integrating gyro yaw if there is one. Returns (elapsed, degrees)."""
        yaw = 0.0
        t0 = time.monotonic()
        last = t0
        self.motors.set(left, right)
        try:
            while True:
                now = time.monotonic()
                if now - t0 >= secs:
                    break
                if self.imu is not None:
                    with contextlib.suppress(Exception):
                        yaw += float(self.imu.read().get("gz_dps", 0.0)) * (now - last)
                last = now
                time.sleep(0.005)
        finally:
            self.motors.stop()
            elapsed = time.monotonic() - t0
        return elapsed, yaw

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.motors.close()
        if self.imu is not None:
            with contextlib.suppress(Exception):
                self.imu.close()


# --- the interactive passes ---------------------------------------------------------------


def ask_float(prompt: str) -> float:
    while True:
        raw = input(prompt).strip()
        if raw.lower() in ("q", "quit", "abort"):
            raise CalibrationError("aborted; calib.yaml untouched")
        try:
            return float(raw)
        except ValueError:
            print("  that is not a number (or 'q' to abort)")


def measure_straight(rig: Rig, secs: float, duties=DUTIES) -> dict[float, float]:
    print("\n--- straight-line speed ---")
    print("Mark a start line. For each duty: line the rover up, it drives, you measure.")
    table: dict[float, float] = {}
    for duty in duties:
        input(f"\n  duty {duty:g}: rover on the start line, room ahead -- Enter to drive ")
        elapsed, _ = rig.run_leg(duty, duty, secs)
        dist = ask_float(f"  measured distance in METRES (ran {elapsed:.2f} s): ")
        table[duty] = speed_from_run(dist, elapsed)
        print(f"  -> {table[duty]:.3f} m/s")
    return table


def measure_turns(rig: Rig, secs: float, duties=DUTIES) -> dict[float, float]:
    print("\n--- turn rate ---")
    if rig.imu is not None:
        print("Gyro present: degrees are integrated from the MPU-6050, not eyeballed.")
    else:
        print("No gyro: you will be asked to measure each spin. Mark the chassis and the floor.")
    table: dict[float, float] = {}
    for duty in duties:
        input(f"\n  duty {duty:g}: clear space around the rover -- Enter to spin ")
        elapsed, yaw = rig.run_leg(-duty, duty, secs)  # CCW: left back, right forward
        if rig.imu is not None and abs(yaw) > 1.0:
            print(f"  gyro says {yaw:+.1f} deg in {elapsed:.2f} s")
            degrees = abs(yaw)
        else:
            degrees = abs(ask_float(f"  measured rotation in DEGREES (ran {elapsed:.2f} s): "))
        table[duty] = rate_from_turn(degrees, elapsed)
        print(f"  -> {table[duty]:.1f} deg/s")
    return table


def measure_trim(rig: Rig, secs: float, track_m: float, duty: float = 0.5) -> float:
    print("\n--- straight-line trim ---")
    print("Drive a long straight leg, then measure how far sideways it ended up.")
    input(f"\n  duty {duty:g}: rover on the line -- Enter to drive ")
    elapsed, _ = rig.run_leg(duty, duty, secs)
    dist = ask_float(f"  distance travelled, METRES (ran {elapsed:.2f} s): ")
    drift = ask_float("  sideways offset, METRES, positive to the rover's LEFT: ")
    trim = trim_from_drift(drift, dist, track_m)
    side = "left" if trim > 0 else "right"
    print(f"  -> motor_trim {trim:+.4f} (slows the {side} wheel)")
    return trim


# --- main ---------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--straight", action="store_true", help="measure m_per_s_at")
    p.add_argument("--turns", action="store_true", help="measure deg_per_s_at")
    p.add_argument("--trim", action="store_true", help="measure motor_trim")
    p.add_argument("--all", action="store_true", help="all three")
    p.add_argument(
        "--secs", type=float, default=DEFAULT_SECS, help=f"per leg (default {DEFAULT_SECS})"
    )
    p.add_argument(
        "--track",
        type=float,
        default=0.13,
        help="wheel track in m, for --trim (UNVERIFIED default)",
    )
    p.add_argument("--sim", action="store_true", help="no GPIO: exercise the arithmetic only")
    p.add_argument("--calib", default=str(CALIB_PATH))
    p.add_argument("--dry-run", action="store_true", help="print, write nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    want = {
        "straight": args.straight or args.all,
        "turns": args.turns or args.all,
        "trim": args.trim or args.all,
    }
    if not any(want.values()):
        build_parser().print_help()
        return 2
    if args.secs <= 0 or args.secs > 10:
        raise CalibrationError(f"--secs {args.secs} is not a sane leg length (0 < secs <= 10)")

    calib = load_calib(args.calib)
    if not args.sim:
        print(
            "\n!! The motors will run. Rover on the floor, room ahead, hand on the kill switch.\n"
            "   There is no watchdog and no tilt cutoff in this tool."
        )
        if input("   type 'go' to continue: ").strip().lower() != "go":
            print("aborted; calib.yaml untouched")
            return 1

    rig = Rig(calib, sim=args.sim)
    measured: dict[str, object] = {}
    try:
        if want["straight"]:
            measured["m_per_s_at"] = measure_straight(rig, args.secs)
        if want["turns"]:
            measured["deg_per_s_at"] = measure_turns(rig, args.secs)
        if want["trim"]:
            measured["motor_trim"] = measure_trim(rig, args.secs, args.track)
    except KeyboardInterrupt:
        rig.motors.stop()
        print("\ninterrupted; calib.yaml untouched")
        return 1
    finally:
        rig.close()

    when = datetime.now(UTC).replace(microsecond=0).isoformat()
    text = Path(args.calib).read_text(encoding="utf-8")
    for key in ("m_per_s_at", "deg_per_s_at"):
        if key in measured:
            table = merge_table(dict(getattr(calib, key)), measured[key])  # type: ignore[arg-type]
            text = calib_edit.replace_table(text, key, table, f"measured {when}")
    if "motor_trim" in measured:
        trim = float(measured["motor_trim"])  # type: ignore[arg-type]
        side = "left" if trim > 0 else "right"
        text = calib_edit.replace_scalar(text, "motor_trim", trim, f"measured: slows the {side}")

    print("\n--- result ---")
    for key, value in measured.items():
        print(f"  {key}: {value}")

    if args.dry_run:
        print("\n--dry-run: calib.yaml not touched")
        return 0
    # Deliberately NOT stamping calibrated_at. That flag is read by `Station.geometry_ok` to
    # decide whether a station may upload a BEV tile and proposal positions, and the thing
    # that makes those meaningful is the homography -- which only calibrate_camera.py
    # measures. Stamping it here would make a station trust an identity homography and
    # upload plausible fiction, which is the one failure calib.yaml's header warns about.
    # (One flag meaning two different kinds of "calibrated" is a smell; noted, not fixed.)
    Path(args.calib).write_text(text, encoding="utf-8")
    print("calibrated_at left alone: that is calibrate_camera.py's to set, not this tool's.")
    print(f"wrote {args.calib}")
    print("re-run `make test`, then re-check a 1 m drive with a tape measure.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CalibrationError as exc:
        print(f"\ncalibration failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
