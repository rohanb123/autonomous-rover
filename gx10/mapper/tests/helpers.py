"""Test images and station bundles for the mapper tests."""

from __future__ import annotations

import io
import json

import numpy as np
from PIL import Image

RUN = "r_20261024_101500"


def noise_jpg(w: int = 1280, h: int = 720, seed: int = 0, quality: int = 90) -> bytes:
    """Random noise: the worst case for JPEG size."""
    arr = np.random.default_rng(seed).integers(0, 256, (h, w, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def bev_png(w: int = 360, h: int = 360, noise: bool = False, alpha: bool = True) -> bytes:
    """BEV-like PNG: brown ground, a transparent wedge the camera didn't see, optional noise."""
    rng = np.random.default_rng(1)
    if noise:
        rgb = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
    else:
        rgb = np.zeros((h, w, 3), dtype=np.uint8)
        rgb[:] = (122, 82, 52)
        rgb[: h // 2, w // 2 - 2 : w // 2 + 2] = (240, 240, 240)  # "forward" stripe
    mode, arr = "RGB", rgb
    if alpha:
        a = np.full((h, w, 1), 255, dtype=np.uint8)
        a[h * 3 // 4 :, :] = 0  # behind the rover: not seen
        mode, arr = "RGBA", np.concatenate([rgb, a], axis=2)
    buf = io.BytesIO()
    Image.fromarray(arr, mode).save(buf, format="PNG")
    return buf.getvalue()


def station_bundle(
    sid: str = "s_0002",
    n_kf: int = 8,
    bev_px: int = 360,
    kf_bytes: bytes | None = None,
    bev_bytes: bytes | None = None,
    drop: tuple[str, ...] = (),
) -> tuple[dict, list]:
    """(station.json dict, httpx `files=` list) shaped like docs/00 §6.4."""
    keyframes = [
        {"kf_id": f"k_{i + 1:04d}", "file": f"kf_{i:03d}.jpg", "heading_deg": 30.0 + 45 * i,
         "t": 1761300101.0 + i}
        for i in range(n_kf)
    ]  # fmt: skip
    meta = {
        "run_id": RUN,
        "station_id": sid,
        "t": 1761300100.0,
        "pose": {"x": 0.8, "y": 0.1, "heading_deg": 30.0, "source": "dr+gyro"},
        "keyframes": keyframes,
        "bev": {"file": "bev.png", "res_m_px": 0.005, "w_px": bev_px, "h_px": bev_px,
                "frame": "rover@station"},
        "proposals": [
            {"prop_id": "p_0031", "kf_id": "k_0002", "bbox_px": [312, 288, 40, 36],
             "crop": "crop_p_0031.jpg", "xy": [1.12, 0.21], "size_m": 0.028, "contrast": 0.41},
            {"prop_id": "p_0032", "kf_id": "k_0003", "bbox_px": [100, 200, 30, 30],
             "crop": "crop_p_0032.jpg", "xy": [0.95, 0.40], "size_m": 0.018, "contrast": 0.35},
        ],
    }  # fmt: skip
    kf = kf_bytes if kf_bytes is not None else noise_jpg()
    parts = {k["file"]: kf for k in keyframes}
    parts["bev.png"] = bev_bytes if bev_bytes is not None else bev_png(bev_px, bev_px)
    parts["crop_p_0031.jpg"] = noise_jpg(96, 96, seed=2)
    parts["crop_p_0032.jpg"] = noise_jpg(96, 96, seed=3)
    files = [("station.json", ("station.json", json.dumps(meta), "application/json"))]
    files += [
        (n, (n, data, "application/octet-stream")) for n, data in parts.items() if n not in drop
    ]
    return meta, files


class RecordingWriter:
    """Stands in for RateLimitedWriter: records reducer calls instead of sending them."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def submit(self, key, reducer, *args) -> None:
        self.calls.append((reducer, *args))

    def by(self, reducer: str) -> list[tuple]:
        return [c[1:] for c in self.calls if c[0] == reducer]
