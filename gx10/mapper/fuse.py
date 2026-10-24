"""Global map fusion, dead-reckoning version (docs/roles/P3 §6, method "dr").

Paints every station's BEV tile into one mission-frame mosaic at its station pose, newest on top,
then slices it into 512 px global tiles. No pose refinement yet: the pose graph (overhead ArUco
+ BEV-overlap yaw edges) comes later and will reuse this painter with corrected poses.

Conventions (all documented, docs/00 §4 and docs/06 §6):
- BEV tile: rover frame at the station, centred on the rover (geometric centre), image up = rover
  +x, image left = rover +y, res_m_px metres per pixel (from station.json).
- Station pose: mission frame (x, y, heading_deg CCW from +x).
- Mosaic / global tiles: axis-aligned, image up = mission +x, image left = mission +y; the origin
  is the mission (x, y) of the top-left pixel corner (max x, max y).

Outputs under the data root: runs/<run_id>/fused/mosaic.png and meta.json (version, bounds, ...).
"""

from __future__ import annotations

import io
import json
import math
import threading
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from PIL import Image

from marsrover_shared.schemas import StationJson

from .store import RunStore, check_id
from .tiles import slice_mosaic

DEFAULT_RES_M_PX = 0.005
MAX_MOSAIC_PX = 8000  # 40 m at 5 mm/px; the arena is ~3 m
_lock = threading.Lock()


class FuseError(ValueError):
    """Nothing to fuse (no stations) or an impossible mosaic."""


def bev_affine(
    pose_x: float,
    pose_y: float,
    heading_deg: float,
    bev_w: int,
    bev_h: int,
    bev_res: float,
    origin: tuple[float, float],
    res: float,
) -> tuple[float, float, float, float, float, float]:
    """PIL AFFINE coefficients mapping a mosaic pixel (col, row) to a BEV pixel (col_b, row_b).

    mosaic (col, row) -> mission x = X0 − row·res, y = Y0 − col·res
    -> rover frame (rotate by −heading about the pose) -> BEV col_b = cx − yr/bev_res,
       row_b = cy − xr/bev_res, with (cx, cy) the BEV image centre.
    """
    x0, y0 = origin
    th = math.radians(heading_deg)
    c, s = math.cos(th), math.sin(th)
    ex, ey = x0 - pose_x, y0 - pose_y
    k = res / bev_res
    cx, cy = bev_w / 2.0, bev_h / 2.0
    a, b = c * k, -s * k
    d, e = s * k, c * k
    c0 = cx + (s * ex - c * ey) / bev_res
    f = cy - (c * ex + s * ey) / bev_res
    return (a, b, c0, d, e, f)


def bev_corners(
    st: StationJson, bev_w: int, bev_h: int, bev_res: float
) -> list[tuple[float, float]]:
    """Mission-frame corners of a station's BEV tile."""
    th = math.radians(st.pose.heading_deg)
    c, s = math.cos(th), math.sin(th)
    hx, hy = bev_h * bev_res / 2.0, bev_w * bev_res / 2.0  # rows run along x, columns along y
    out = []
    for xr, yr in ((hx, hy), (hx, -hy), (-hx, hy), (-hx, -hy)):
        out.append((st.pose.x + c * xr - s * yr, st.pose.y + s * xr + c * yr))
    return out


def _load_stations(
    store: RunStore, run_id: str
) -> tuple[list[tuple[StationJson, Image.Image]], list[str]]:
    base = store.root / "runs" / check_id(run_id, "run_id") / "stations"
    stations, warnings = [], []
    for meta_path in sorted(base.glob("*/station.json")):
        try:
            st = StationJson.model_validate_json(meta_path.read_text(encoding="utf-8"))
            bev = Image.open(io.BytesIO((meta_path.parent / st.bev.file).read_bytes()))
            bev.load()
        except Exception as e:  # missing/broken file: fuse the rest
            warnings.append(f"{meta_path.parent.name}: skipped ({type(e).__name__}: {e})")
            continue
        stations.append((st, bev.convert("RGBA")))
    stations.sort(key=lambda sb: sb[0].t)  # oldest first, newest painted on top
    return stations, warnings


def _next_version(fused_dir: Path) -> int:
    try:
        return int(json.loads((fused_dir / "meta.json").read_text(encoding="utf-8"))["version"]) + 1
    except (OSError, ValueError, KeyError):
        return 1


def fuse_run(
    run_id: str, res_m_px: float = DEFAULT_RES_M_PX, store: RunStore | None = None
) -> dict:
    """Mosaic + global tiles for a run. Returns {tiles, bounds, method, version, origin, ...}."""
    store = store or RunStore()
    with _lock:
        stations, warnings = _load_stations(store, run_id)
        if not stations:
            raise FuseError(f"no readable stations for {run_id}")

        corners = [
            p
            for st, bev in stations
            for p in bev_corners(st, bev.width, bev.height, st.bev.res_m_px)
        ]
        xs, ys = [p[0] for p in corners], [p[1] for p in corners]
        min_x, max_x, min_y, max_y = min(xs), max(xs), min(ys), max(ys)
        w = max(1, math.ceil((max_y - min_y) / res_m_px))
        h = max(1, math.ceil((max_x - min_x) / res_m_px))
        if max(w, h) > MAX_MOSAIC_PX:
            raise FuseError(f"mosaic would be {w}×{h} px; poses span too far for {res_m_px} m/px")
        origin = (max_x, max_y)

        mosaic = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        for st, bev in stations:
            coeffs = bev_affine(
                st.pose.x, st.pose.y, st.pose.heading_deg,
                bev.width, bev.height, st.bev.res_m_px, origin, res_m_px,
            )  # fmt: skip
            warped = bev.transform(
                (w, h), Image.Transform.AFFINE, coeffs, resample=Image.Resampling.BILINEAR,
                fillcolor=(0, 0, 0, 0),
            )  # fmt: skip
            mosaic.alpha_composite(warped)

        fused_dir = store.root / "runs" / run_id / "fused"
        fused_dir.mkdir(parents=True, exist_ok=True)
        version = _next_version(fused_dir)
        mosaic.save(fused_dir / "mosaic.png")
        tiles = slice_mosaic(np.asarray(mosaic), origin, res_m_px)
        bounds = {"min_x": min_x, "max_x": max_x, "min_y": min_y, "max_y": max_y}
        meta = {
            "run_id": run_id,
            "version": version,
            "method": "dr",
            "res_m_px": res_m_px,
            "origin": list(origin),
            "size_px": [w, h],
            "bounds": bounds,
            "stations": [st.station_id for st, _ in stations],
            "tiles": len(tiles),
            "warnings": warnings,
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        (fused_dir / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return {**meta, "tiles": tiles, "mosaic": f"runs/{run_id}/fused/mosaic.png"}
