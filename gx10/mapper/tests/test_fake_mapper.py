"""mocks/fake_mapper.py speaks the docs/00 §6.4-6.5 contract and the docs/02 reducer args."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from marsrover_shared.schemas import MapperStationReply, SampleReply

_MOCK = Path(__file__).resolve().parents[3] / "mocks" / "fake_mapper.py"
RUN = "r_20261024_101500"
PUBLIC = "http://mapper.test:8200"


class RecordingWriter:
    """Stands in for RateLimitedWriter: records reducer calls instead of sending them."""

    def __init__(self):
        self.calls: list[tuple] = []

    def submit(self, key, reducer, *args):
        self.calls.append((reducer, *args))

    def by(self, reducer):
        return [c[1:] for c in self.calls if c[0] == reducer]


def _load():
    spec = importlib.util.spec_from_file_location("fake_mapper", _MOCK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MARSROVER_DATA_DIR", str(tmp_path))
    writer = RecordingWriter()
    client = TestClient(_load().create_app(tmp_path, PUBLIC, writer))
    return client, writer, tmp_path


def _jpg(w=640, h=480) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (90, 60, 40)).save(buf, format="JPEG")
    return buf.getvalue()


def _png(w=360, h=360) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 80, 50)).save(buf, format="PNG")
    return buf.getvalue()


def _station(sid="s_0002", n_kf=8, bev_px=360):
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
             "crop": "crop_p_0031.jpg", "xy": [1.12, 0.21], "size_m": 0.028, "contrast": 0.41}
        ],
    }  # fmt: skip
    files = [("station.json", ("station.json", json.dumps(meta), "application/json"))]
    files += [(kf["file"], (kf["file"], _jpg(), "image/jpeg")) for kf in keyframes]
    files += [
        ("bev.png", ("bev.png", _png(bev_px, bev_px), "image/png")),
        ("crop_p_0031.jpg", ("crop_p_0031.jpg", _jpg(96, 96), "image/jpeg")),
    ]
    return meta, files


def test_station_upload_stores_files_and_replies_per_contract(env):
    client, _, root = env
    _, files = _station()
    t0 = time.perf_counter()
    r = client.post("/ingest/station", files=files)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200, r.text
    reply = MapperStationReply.model_validate(r.json())
    base = f"{PUBLIC}/files/runs/{RUN}/stations/s_0002"
    assert reply.ok and reply.station_id == "s_0002"
    assert reply.stored == f"/data/runs/{RUN}/stations/s_0002/"
    assert reply.urls.bev == f"{base}/bev.png"
    assert reply.urls.crops == {"p_0031": f"{base}/crop_p_0031.jpg"}
    stored = {p.name for p in (root / "runs" / RUN / "stations" / "s_0002").iterdir()}
    assert stored >= {"station.json", "bev.png", "kf_007.jpg", "crop_p_0031.jpg"}
    assert elapsed < 1.0  # P3 acceptance: 8 keyframes + BEV reply in < 1 s


def test_station_writes_keyframes_and_local_tile_with_positional_args(env):
    client, writer, _ = env
    _, files = _station()
    client.post("/ingest/station", files=files)
    kfs = writer.by("upsert_keyframe")
    assert len(kfs) == 8
    kf_id, run_id, sid, t, x, y, hdg, thumb, url = kfs[1]
    assert (kf_id, run_id, sid, t, x, y, hdg) == (
        "k_0002", RUN, "s_0002", 1761300102.0, 0.8, 0.1, 75.0,
    )  # fmt: skip
    assert url.endswith("/kf_001.jpg") and len(thumb) <= 150_000
    img = Image.open(io.BytesIO(base64.b64decode(thumb)))
    assert img.format == "JPEG" and max(img.size) <= 320

    (tile_args,) = writer.by("upsert_local_tile")
    sid, run_id, t, x, y, hdg, res, w, h, png, url = tile_args
    assert (sid, run_id, t, x, y, hdg, res, w, h) == (
        "s_0002", RUN, 1761300100.0, 0.8, 0.1, 30.0, 0.005, 360, 360,
    )  # fmt: skip
    assert len(png) <= 700_000 and url.endswith("/bev.png")
    assert Image.open(io.BytesIO(base64.b64decode(png))).size == (360, 360)
    assert writer.by("log_event")[0][2] == "mapper"


def test_large_bev_tile_is_capped_at_512px_keeping_metric_size(env):
    client, writer, _ = env
    _, files = _station(bev_px=1024)
    client.post("/ingest/station", files=files)
    (args,) = writer.by("upsert_local_tile")
    res, w, h = args[6:9]
    assert (w, h) == (512, 512)
    assert abs(res * w - 0.005 * 1024) < 1e-9


def test_files_endpoint_serves_uploads_and_blocks_traversal(env):
    client, _, _ = env
    _, files = _station()
    client.post("/ingest/station", files=files)
    r = client.get(f"/files/runs/{RUN}/stations/s_0002/bev.png")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert client.get("/files/..%2F..%2Fpyproject.toml").status_code == 404
    assert client.get("/files/runs/nope.png").status_code == 404


def test_station_rejects_bad_metadata(env):
    client, writer, _ = env
    meta, files = _station()
    bad = dict(meta, run_id="../evil")
    bad_files = [("station.json", ("station.json", json.dumps(bad), "application/json"))]
    assert client.post("/ingest/station", files=bad_files + files[1:]).status_code == 422
    assert client.post("/ingest/station", files=files[1:]).status_code == 422
    assert writer.calls == []


def test_sample_upload(env):
    client, _, root = env
    meta = {
        "run_id": RUN,
        "sample_id": "a_0001",
        "cand_id": "c_0001",
        "t": 1761300200.0,
        "pose": {"x": 1.0, "y": 0.2, "heading_deg": 10.0, "source": "dr"},
    }
    files = [
        ("sample.json", ("sample.json", json.dumps(meta), "application/json")),
        ("closeup.jpg", ("closeup.jpg", _jpg(), "image/jpeg")),
    ]
    reply = SampleReply.model_validate(client.post("/ingest/sample", files=files).json())
    assert reply.url == f"{PUBLIC}/files/runs/{RUN}/samples/a_0001/closeup.jpg"
    assert (root / "runs" / RUN / "samples" / "a_0001" / "closeup.jpg").is_file()
    assert client.post("/ingest/sample", files=files[:1]).status_code == 422


def test_fuse_writes_global_tile_and_bumps_version(env):
    client, writer, _ = env
    assert client.post(f"/fuse/{RUN}").json() == {"ok": True, "tiles": 1}
    client.post(f"/fuse/{RUN}")
    tiles = writer.by("upsert_global_tile")
    assert [t[0] for t in tiles] == [f"g_{RUN}_0_0"] * 2
    assert [t[-1] for t in tiles] == [1, 2]
    _, _, ix, iy, ox, oy, res, w, h, _, version = tiles[0]
    assert (ox, oy) == (1.28, 1.28)  # top-left corner of a tile centered on home
    assert (ix, iy, res, w, h) == (0, 0, 0.005, 512, 512) and isinstance(version, int)
    assert writer.by("set_run_fused") == [(RUN, True)] * 2


def test_events_jsonl_written(env):
    client, _, root = env
    _, files = _station()
    client.post("/ingest/station", files=files)
    lines = (root / "runs" / RUN / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["kind"] == "station_stored"


def test_health_reports_stdb_writer(env):
    client, writer, _ = env
    assert client.get("/health").json() == {"ok": True, "stdb": True}
    writer.dropped = 1  # a write was dropped
    assert client.get("/health").json() == {"ok": True, "stdb": False}
