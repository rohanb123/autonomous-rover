"""Measure the ground-plane homography, and write it into calib.yaml.

This is what turns a station from "keyframes only" into a real survey: until it has run,
`calibrated_at` is null, the homography is an identity placeholder, and `station.py` uploads
photographs without a BEV tile or proposals because every `xy` would be fiction.

How (P2 role doc section 6):
  1. Lay a rectangle of KNOWN size flat on the ground in front of the rover. A sheet of A4
     is 0.297 x 0.210 m; `--width`/`--depth` default to that, landscape.
  2. Measure the gap from the rover's centre of rotation to the rectangle's near edge and
     pass it as `--near`. This is the number people get wrong: it is the centre of
     rotation, not the front of the chassis, because that is where the BEV square is
     centred (see rover/rover/bev.py).
  3. Run this. Click the four corners in the order it prints, or pass `--corners`.
  4. It checks the fit, then rewrites `homography` and `calibrated_at` in calib.yaml,
     leaving every comment in the file alone.

Verify afterwards, as the role doc asks: a 10 cm square should measure 10 +/- 1 cm in the
BEV. `--check` does exactly that from an independent fifth point.

    python rover/calib/calibrate_camera.py --camera picamera2 --near 0.18
    python rover/calib/calibrate_camera.py --image frame.jpg \
        --corners "100,400 540,400 420,300 220,300" \
        --check "320,350,0.25,0.0"
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np

_ROOT = Path(__file__).resolve().parents[2]
for _p in (_ROOT / "shared" / "py", _ROOT / "rover"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from rover.bev import pixel_to_rover_xy, rover_to_bev_px  # noqa: E402
from rover.config import load_calib  # noqa: E402

CALIB_PATH = _ROOT / "rover" / "calib" / "calib.yaml"

#: The order the corners must be given in. Printed before any clicking, because getting it
#: wrong produces a homography that is a valid matrix and complete nonsense.
CORNER_ORDER = (
    "near-left  (closest to the rover, on its LEFT)",
    "near-right (closest to the rover, on its RIGHT)",
    "far-right",
    "far-left",
)

#: A 10 cm edge must measure 10 +/- 1 cm (role doc section 6).
TOLERANCE_M = 0.01


class CalibrationError(RuntimeError):
    """The fit is not good enough to write. Better no homography than a wrong one."""


# --- geometry ------------------------------------------------------------------------------


def target_corners_m(near_m: float, width_m: float, depth_m: float) -> list[tuple[float, float]]:
    """Where the four corners ARE, in rover-frame metres, in CORNER_ORDER.

    Rover frame: +x forward, +y left (doc 00 section 4). The rectangle is centred laterally.
    """
    half = width_m / 2.0
    far = near_m + depth_m
    return [(near_m, half), (near_m, -half), (far, -half), (far, half)]


def homography_from_clicks(
    clicks: list[tuple[float, float]],
    near_m: float,
    width_m: float,
    depth_m: float,
    res_m_px: float,
    size_px: int,
) -> np.ndarray:
    """Image pixels -> BEV pixels, from four clicked corners of a known rectangle."""
    if len(clicks) != 4:
        raise CalibrationError(f"need exactly 4 corners, got {len(clicks)}")
    _check_quad(clicks)
    dst = [
        rover_to_bev_px(fwd, lat, res_m_px, size_px)
        for fwd, lat in target_corners_m(near_m, width_m, depth_m)
    ]
    _check_in_bounds(dst, size_px, near_m, width_m, depth_m, res_m_px)
    H = cv2.getPerspectiveTransform(
        np.array(clicks, dtype=np.float32), np.array(dst, dtype=np.float32)
    )
    if abs(float(np.linalg.det(H))) < 1e-12:
        raise CalibrationError("the fit is singular; the four corners are probably collinear")
    return H


def _check_quad(clicks: list[tuple[float, float]]) -> None:
    """The clicked points must form a convex quad traversed consistently.

    This is the check that earns its keep: clicking the corners in the wrong order (or
    diagonally) yields a perfectly invertible matrix that maps the ground to a bow tie, and
    nothing downstream can tell.
    """
    pts = np.array(clicks, dtype=np.float64)
    signs = []
    for i in range(4):
        a, b, c = pts[i], pts[(i + 1) % 4], pts[(i + 2) % 4]
        # The 2-D cross product written out: np.cross on 2-vectors was removed in NumPy 2.
        (ux, uy), (vx, vy) = b - a, c - b
        cross = float(ux * vy - uy * vx)
        if abs(cross) < 1e-9:
            raise CalibrationError(
                f"corners {i + 1}, {i + 2} and {(i + 2) % 4 + 1} are collinear; "
                "re-click them further apart"
            )
        signs.append(cross > 0)
    if len(set(signs)) != 1:
        raise CalibrationError(
            "the four corners do not form a convex quadrilateral, which means they were "
            "clicked out of order. The order is:\n  "
            + "\n  ".join(f"{i + 1}. {name}" for i, name in enumerate(CORNER_ORDER))
        )


def _check_in_bounds(
    dst: list[tuple[float, float]],
    size_px: int,
    near_m: float,
    width_m: float,
    depth_m: float,
    res_m_px: float,
) -> None:
    """The target must land inside the BEV square, or the warp silently clips it."""
    if all(0 <= u < size_px and 0 <= v < size_px for u, v in dst):
        return
    reach = size_px * res_m_px / 2.0
    raise CalibrationError(
        f"a {width_m:g} x {depth_m:g} m target {near_m:g} m out does not fit in the BEV "
        f"square, which reaches {reach:.2f} m from the rover "
        f"(bev_size_px {size_px} x bev_res_m_px {res_m_px:g}). Move the target closer, or "
        "raise bev_size_px in calib.yaml"
    )


def residuals_m(
    H: np.ndarray,
    clicks: list[tuple[float, float]],
    near_m: float,
    width_m: float,
    depth_m: float,
    res_m_px: float,
    size_px: int,
) -> list[float]:
    """How far each recovered corner is from where it should be, in metres.

    Four points through getPerspectiveTransform is an exact fit, so these are ~0 by
    construction. They are reported anyway: a residual that is NOT tiny means the maths or
    the convention disagrees with itself, which is worth knowing before writing the file.
    `--check` is the independent measurement.
    """
    want = target_corners_m(near_m, width_m, depth_m)
    out = []
    for (u, v), (fwd, lat) in zip(clicks, want, strict=True):
        got = pixel_to_rover_xy(u, v, H, res_m_px, size_px)
        out.append(float(np.hypot(got[0] - fwd, got[1] - lat)))
    return out


def check_point_error_m(
    H: np.ndarray,
    uv: tuple[float, float],
    expect_m: tuple[float, float],
    res_m_px: float,
    size_px: int,
) -> tuple[tuple[float, float], float]:
    """An INDEPENDENT point: where the homography says it is, and how far that is off."""
    got = pixel_to_rover_xy(uv[0], uv[1], H, res_m_px, size_px)
    return got, float(np.hypot(got[0] - expect_m[0], got[1] - expect_m[1]))


# --- calib.yaml, comments intact -----------------------------------------------------------


def rewrite_calib(text: str, H: np.ndarray, when: str) -> str:
    """Replace the `homography` rows and `calibrated_at` in place.

    Deliberately textual rather than a yaml round-trip: calib.yaml is half comments, and
    PyYAML would drop every one of them. The surrounding file is not ours to reformat.
    """
    rows = "\n".join(
        "    - [{:.8f}, {:.8f}, {:.8f}]".format(*row) for row in np.asarray(H, dtype=float)
    )
    pattern = re.compile(
        r"^(?P<indent>[ \t]*)homography:.*(?:\r?\n(?:[ \t]*-[^\r\n]*|[ \t]*#[^\r\n]*))+",
        re.MULTILINE,
    )
    if not pattern.search(text):
        raise CalibrationError("could not find the homography block in calib.yaml")
    replacement = f"  homography:               # measured by calibrate_camera.py on {when}\n{rows}"
    text = pattern.sub(lambda _m: replacement, text, count=1)

    stamp = re.compile(r"^calibrated_at:[^\r\n]*", re.MULTILINE)
    if not stamp.search(text):
        raise CalibrationError("could not find calibrated_at in calib.yaml")
    return stamp.sub(
        f'calibrated_at: "{when}"   # set by calibrate_camera.py; null = nothing is measured.',
        text,
        count=1,
    )


# --- input ---------------------------------------------------------------------------------


def parse_corners(spec: str) -> list[tuple[float, float]]:
    """`"u,v u,v u,v u,v"` -> four points, for a headless run."""
    out = []
    for token in spec.replace(";", " ").split():
        try:
            u, v = (float(x) for x in token.split(","))
        except ValueError as e:
            raise CalibrationError(f"cannot read {token!r} as u,v") from e
        out.append((u, v))
    if len(out) != 4:
        raise CalibrationError(f"--corners needs 4 points, got {len(out)}")
    return out


def click_corners(img: np.ndarray) -> list[tuple[float, float]]:
    """Open a window and collect four clicks. Backspace undoes, Enter accepts."""
    clicks: list[tuple[float, float]] = []
    window = "click the corners  --  backspace undoes, enter accepts, esc aborts"

    def on_mouse(event: int, x: int, y: int, flags: int, userdata: object) -> None:
        if event == cv2.EVENT_LBUTTONDOWN and len(clicks) < 4:
            clicks.append((float(x), float(y)))

    cv2.namedWindow(window)
    cv2.setMouseCallback(window, on_mouse)
    try:
        while True:
            shown = img.copy()
            for i, (u, v) in enumerate(clicks):
                cv2.circle(shown, (int(u), int(v)), 5, (0, 0, 255), -1)
                cv2.putText(
                    shown,
                    str(i + 1),
                    (int(u) + 8, int(v) - 8),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    (0, 0, 255),
                    2,
                )
            if len(clicks) == 4:
                cv2.polylines(shown, [np.array(clicks, dtype=np.int32)], True, (0, 255, 0), 2)
            label = (
                "all four: enter to accept"
                if len(clicks) == 4
                else f"click {len(clicks) + 1}: {CORNER_ORDER[len(clicks)]}"
            )
            cv2.putText(shown, label, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
            cv2.imshow(window, shown)
            key = cv2.waitKey(30) & 0xFF
            if key in (8, 127) and clicks:  # backspace
                clicks.pop()
            elif key in (13, 10) and len(clicks) == 4:  # enter
                return clicks
            elif key == 27:  # esc
                raise CalibrationError("aborted at the clicking step; calib.yaml untouched")
    finally:
        cv2.destroyWindow(window)


def grab_frame(args: argparse.Namespace, cfg: object) -> np.ndarray:
    """A frame from --image, or from the camera with a locked exposure."""
    if args.image:
        img = cv2.imread(args.image)
        if img is None:
            raise CalibrationError(f"cannot read {args.image}")
        return img
    from rover.camera import Camera

    camera = Camera(cfg)  # type: ignore[arg-type]
    camera.open(args.camera)
    if not camera.present:
        raise CalibrationError(f"no camera on the {args.camera!r} backend")
    try:
        camera.focus_for_survey()
        camera.lock_exposure()  # same conditions a station will use
        return camera.grab()
    finally:
        camera.close()


# --- main ----------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--near",
        type=float,
        required=True,
        help="metres from the rover's CENTRE OF ROTATION to the target's near edge",
    )
    p.add_argument("--width", type=float, default=0.297, help="target width, m (A4 landscape)")
    p.add_argument("--depth", type=float, default=0.210, help="target depth, m (A4 landscape)")
    p.add_argument("--camera", default="auto", help="auto|picamera2|opencv|sim")
    p.add_argument("--image", default=None, help="use a saved frame instead of the camera")
    p.add_argument(
        "--corners", default=None, help='headless: "u,v u,v u,v u,v" in the printed order'
    )
    p.add_argument(
        "--check",
        default=None,
        metavar="u,v,fwd,lat",
        help="an INDEPENDENT point: its pixel and its true rover-frame metres",
    )
    p.add_argument(
        "--save-frame", default=None, help="also write the frame here, to re-run offline"
    )
    p.add_argument("--calib", default=str(CALIB_PATH), help=f"default {CALIB_PATH}")
    p.add_argument("--dry-run", action="store_true", help="print the homography, write nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    calib = load_calib(args.calib)
    res_m_px, size_px = calib.bev_res_m_px, calib.bev_size_px

    print("Target corners, in this order:")
    for i, name in enumerate(CORNER_ORDER):
        print(f"  {i + 1}. {name}")
    print(
        f"\nTarget: {args.width:g} x {args.depth:g} m, near edge {args.near:g} m from the "
        f"centre of rotation.\nBEV: {size_px} px square at {res_m_px:g} m/px "
        f"(reaches {size_px * res_m_px / 2:.2f} m).\n"
    )

    img = grab_frame(args, calib.camera)
    if args.save_frame:
        cv2.imwrite(args.save_frame, img)
        print(f"frame saved to {args.save_frame}")

    clicks = parse_corners(args.corners) if args.corners else click_corners(img)
    print(f"corners: {[(round(u, 1), round(v, 1)) for u, v in clicks]}")

    H = homography_from_clicks(clicks, args.near, args.width, args.depth, res_m_px, size_px)
    worst = max(residuals_m(H, clicks, args.near, args.width, args.depth, res_m_px, size_px))
    print(f"fit residual (exact by construction): {worst * 100:.2f} cm")
    if worst > TOLERANCE_M:
        raise CalibrationError(
            f"the fit does not even reproduce its own corners to {TOLERANCE_M * 100:g} cm "
            f"({worst * 100:.1f} cm). Something is wrong with the geometry, not the clicking"
        )

    if args.check:
        parts = [float(x) for x in args.check.split(",")]
        if len(parts) != 4:
            raise CalibrationError("--check wants u,v,fwd,lat")
        got, err = check_point_error_m(
            H, (parts[0], parts[1]), (parts[2], parts[3]), res_m_px, size_px
        )
        print(
            f"independent check: expected ({parts[2]:.3f}, {parts[3]:.3f}) m, "
            f"got ({got[0]:.3f}, {got[1]:.3f}) m -- off by {err * 100:.2f} cm"
        )
        if err > TOLERANCE_M:
            raise CalibrationError(
                f"the check point is off by {err * 100:.1f} cm, over the {TOLERANCE_M * 100:g} "
                "cm the role doc allows. NOT writing calib.yaml: a silently wrong homography "
                "is worse than none, because the station would upload plausible fiction"
            )
    else:
        print(
            "no --check given: a four-point fit always reproduces its own corners, so "
            "nothing here is an independent measurement yet. Verify by hand before trusting "
            "a station (role doc section 6: a 10 cm square measures 10 +/- 1 cm)."
        )

    print("\nhomography (image px -> BEV px):")
    for row in H:
        print("  [{: .8f}, {: .8f}, {: .8f}]".format(*row))

    if args.dry_run:
        print("\n--dry-run: calib.yaml not touched")
        return 0

    when = datetime.now(UTC).replace(microsecond=0).isoformat()
    path = Path(args.calib)
    path.write_text(rewrite_calib(path.read_text(encoding="utf-8"), H, when), encoding="utf-8")
    print(f"\nwrote homography and calibrated_at: {when} to {path}")
    print("a station will now build a BEV tile and propose rocks. Re-run `make test`.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CalibrationError as exc:
        print(f"\ncalibration failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
