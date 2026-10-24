"""Dead-reckoned global map fusion: geometry, layering, slicing, versions, /fuse endpoint."""

from __future__ import annotations

import base64
import io
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from ..app import create_app
from ..fuse import FuseError, fuse_run
from ..store import RunStore
from ..tiles import slice_mosaic
from .helpers import RUN, RecordingWriter

RES = 0.005


def _bev(size_px=200, dots=(), fill=(0, 0, 0, 0)) -> bytes:
    """BEV PNG (rover frame, image up = forward, image left = rover left) with coloured dots.

    dots: ((forward_m, left_m), (r, g, b)) relative to the rover at the image centre.
    """
    img = Image.new("RGBA", (size_px, size_px), fill)
    px = img.load()
    c = size_px / 2
    for (fwd, left), rgb in dots:
        col, row = int(c - left / RES), int(c - fwd / RES)
        for dr in range(-3, 3):
            for dc in range(-3, 3):
                px[col + dc, row + dr] = (*rgb, 255)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _station(store: RunStore, sid: str, x: float, y: float, hdg: float, t: float, bev: bytes):
    meta = {
        "run_id": RUN, "station_id": sid, "t": t,
        "pose": {"x": x, "y": y, "heading_deg": hdg, "source": "dr"},
        "keyframes": [],
        "bev": {"file": "bev.png", "res_m_px": RES, "w_px": 200, "h_px": 200,
                "frame": "rover@station"},
        "proposals": [],
    }  # fmt: skip
    store.save_station(RUN, meta, {"bev.png": bev})


def _world_of(result: dict, rgb) -> tuple[float, float]:
    """Mission (x, y) of the centroid of pixels of colour rgb in the fused mosaic."""
    mosaic = np.asarray(Image.open(result["_mosaic_path"]).convert("RGBA")).astype(int)
    mask = (np.abs(mosaic[..., :3] - np.array(rgb)).sum(axis=2) < 60) & (mosaic[..., 3] > 128)
    rows, cols = np.nonzero(mask)
    assert len(rows), f"colour {rgb} not found in mosaic"
    x0, y0 = result["origin"]
    return x0 - (rows.mean() + 0.5) * RES, y0 - (cols.mean() + 0.5) * RES


def _fuse(store):
    r = fuse_run(RUN, RES, store)
    r["_mosaic_path"] = store.root / r["mosaic"]
    return r


@pytest.mark.parametrize("hdg", [0.0, 90.0, 180.0, -90.0, 30.0])
def test_bev_points_land_at_their_mission_position(tmp_path, hdg):
    store = RunStore(tmp_path)
    red, blue = (255, 0, 0), (0, 0, 255)
    # red 0.3 m ahead of the rover, blue 0.2 m to its left
    _station(
        store, "s_0001", 1.0, 0.5, hdg, 1.0, _bev(dots=(((0.3, 0.0), red), ((0.0, 0.2), blue)))
    )
    r = _fuse(store)
    th = np.radians(hdg)
    for (fwd, left), rgb in (((0.3, 0.0), red), ((0.0, 0.2), blue)):
        ex = 1.0 + np.cos(th) * fwd - np.sin(th) * left
        ey = 0.5 + np.sin(th) * fwd + np.cos(th) * left
        x, y = _world_of(r, rgb)
        assert (x, y) == pytest.approx((ex, ey), abs=0.012), f"heading {hdg}: {rgb}"


def test_heading_90_forward_points_to_mission_plus_y(tmp_path):
    # Same check as docs/06 D10, in the fused map: forward at heading 90 is +y.
    store = RunStore(tmp_path)
    _station(store, "s_0001", 0.0, 0.0, 90.0, 1.0, _bev(dots=(((0.4, 0.0), (255, 0, 0)),)))
    x, y = _world_of(_fuse(store), (255, 0, 0))
    assert x == pytest.approx(0.0, abs=0.012) and y == pytest.approx(0.4, abs=0.012)


def test_newest_station_is_painted_on_top(tmp_path):
    store = RunStore(tmp_path)
    _station(store, "s_0002", 0.0, 0.0, 0.0, 2.0, _bev(fill=(0, 200, 0, 255)))  # newer: green
    _station(store, "s_0001", 0.1, 0.0, 0.0, 1.0, _bev(fill=(200, 0, 0, 255)))  # older: red
    r = _fuse(store)
    mosaic = np.asarray(Image.open(r["_mosaic_path"]).convert("RGBA"))
    x0, y0 = r["origin"]
    row, col = int((x0 - 0.05) / RES), int((y0 - 0.0) / RES)  # inside both tiles
    assert tuple(mosaic[row, col, :3]) == (0, 200, 0)
    assert r["stations"] == ["s_0001", "s_0002"]  # painted oldest first


def test_bounds_and_size_cover_all_tiles(tmp_path):
    store = RunStore(tmp_path)
    _station(store, "s_0001", 0.0, 0.0, 0.0, 1.0, _bev(fill=(9, 9, 9, 255)))
    _station(store, "s_0002", 2.0, 1.0, 0.0, 2.0, _bev(fill=(9, 9, 9, 255)))
    r = _fuse(store)
    b = r["bounds"]
    assert (b["min_x"], b["max_x"], b["min_y"], b["max_y"]) == pytest.approx((-0.5, 2.5, -0.5, 1.5))
    assert r["origin"] == pytest.approx([2.5, 1.5])
    assert r["size_px"] == [400, 600]  # w along y (2 m), h along x (3 m)
    assert r["method"] == "dr"


def test_versions_increase_on_refuse(tmp_path):
    store = RunStore(tmp_path)
    _station(store, "s_0001", 0.0, 0.0, 0.0, 1.0, _bev(fill=(9, 9, 9, 255)))
    assert fuse_run(RUN, RES, store)["version"] == 1
    assert fuse_run(RUN, RES, store)["version"] == 2
    meta = json.loads((tmp_path / "runs" / RUN / "fused" / "meta.json").read_text(encoding="utf-8"))
    assert meta["version"] == 2 and meta["tiles"] >= 1


def test_broken_station_is_skipped_with_warning(tmp_path):
    store = RunStore(tmp_path)
    _station(store, "s_0001", 0.0, 0.0, 0.0, 1.0, _bev(fill=(9, 9, 9, 255)))
    _station(store, "s_0002", 1.0, 0.0, 0.0, 2.0, b"not a png")
    r = fuse_run(RUN, RES, store)
    assert r["stations"] == ["s_0001"] and "s_0002" in r["warnings"][0]


def test_no_stations_raises(tmp_path):
    with pytest.raises(FuseError):
        fuse_run(RUN, RES, RunStore(tmp_path))


def test_slice_mosaic_origins_follow_the_corner_convention():
    mosaic = np.zeros((700, 1100, 4), dtype=np.uint8)
    mosaic[:, :, 3] = 255
    mosaic[600:, 1000:, 3] = 0  # bottom-right block fully transparent
    mosaic[512:, 1024:, 3] = 0
    tiles = slice_mosaic(mosaic, (2.0, 3.0), RES)
    by = {(t["ix"], t["iy"]): t for t in tiles}
    assert (2, 1) not in by  # empty tile skipped
    assert by[(0, 0)]["origin_x"] == 2.0 and by[(0, 0)]["origin_y"] == 3.0
    assert by[(1, 0)]["origin_y"] == pytest.approx(3.0 - 512 * RES)  # next column: smaller y
    assert by[(0, 1)]["origin_x"] == pytest.approx(2.0 - 512 * RES)  # next row: smaller x
    assert (by[(2, 0)]["w_px"], by[(0, 1)]["h_px"]) == (1100 - 1024, 700 - 512)
    img = Image.open(io.BytesIO(base64.b64decode(by[(0, 0)]["png_b64"])))
    assert img.size == (512, 512)


# --- /fuse endpoint -----------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MARSROVER_DATA_DIR", str(tmp_path))
    writer = RecordingWriter()
    store = RunStore(tmp_path, "http://mapper.test:8200")
    return TestClient(create_app(store, writer)), writer, store


def test_fuse_endpoint_writes_tiles_and_marks_run_fused(env):
    client, writer, store = env
    _station(store, "s_0001", 0.0, 0.0, 0.0, 1.0, _bev(fill=(120, 80, 50, 255)))
    _station(store, "s_0002", 0.6, 0.3, 45.0, 2.0, _bev(fill=(120, 80, 50, 255)))
    body = client.post(f"/fuse/{RUN}").json()
    tiles = writer.by("upsert_global_tile")
    assert body == {"ok": True, "tiles": len(tiles)} and len(tiles) >= 1
    tile_id, run_id, ix, iy, ox, oy, res, w, h, png, version = tiles[0]
    assert tile_id == f"g_{RUN}_{ix}_{iy}" and run_id == RUN and res == RES and version == 1
    assert isinstance(ix, int) and isinstance(w, int) and len(png) <= 700_000
    assert writer.by("set_run_fused") == [(RUN, True)]
    ev = writer.by("log_event")[-1]
    assert ev[4] == "fused" and json.loads(ev[6])["method"] == "dr"
    client.post(f"/fuse/{RUN}")
    assert writer.by("upsert_global_tile")[-1][-1] == 2  # version bumped on re-fuse
    mosaic_url = json.loads(ev[6])["mosaic"].removeprefix("http://mapper.test:8200")
    assert client.get(mosaic_url).content[:4] == b"\x89PNG"


def test_fuse_endpoint_errors(env):
    client, writer, _ = env
    assert client.post(f"/fuse/{RUN}").status_code == 404  # no stations yet
    assert client.post("/fuse/not_a_run").status_code == 422
    assert writer.by("set_run_fused") == []
