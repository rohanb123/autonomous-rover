"""Real mapper app: docs/00 §6.4-6.5 contract, docs/02 reducer arg order, P3 acceptance timing."""

from __future__ import annotations

import base64
import io
import json
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from marsrover_shared.schemas import MapperStationReply, SampleReply

from ..app import create_app
from ..store import RunStore
from .helpers import RUN, RecordingWriter, bev_png, noise_jpg, station_bundle

PUBLIC = "http://mapper.test:8200"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MARSROVER_DATA_DIR", str(tmp_path))
    writer = RecordingWriter()
    client = TestClient(create_app(RunStore(tmp_path, PUBLIC), writer))
    return client, writer, tmp_path


def _b64img(s: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(s)))


def _events(root):
    path = root / "runs" / RUN / "events.jsonl"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]


# --- /ingest/station ---------------------------------------------------------------------


def test_station_reply_matches_contract(env):
    client, _, root = env
    _, files = station_bundle()
    r = client.post("/ingest/station", files=files)
    assert r.status_code == 200, r.text
    reply = MapperStationReply.model_validate(r.json())
    base = f"{PUBLIC}/files/runs/{RUN}/stations/s_0002"
    assert reply.ok and reply.station_id == "s_0002"
    assert reply.stored == f"/data/runs/{RUN}/stations/s_0002/"
    assert reply.urls.bev == f"{base}/bev.png"
    assert reply.urls.crops == {
        "p_0031": f"{base}/crop_p_0031.jpg",
        "p_0032": f"{base}/crop_p_0032.jpg",
    }
    names = {p.name for p in (root / "runs" / RUN / "stations" / "s_0002").iterdir()}
    assert names >= {"station.json", "bev.png", "kf_000.jpg", "kf_007.jpg", "crop_p_0032.jpg"}


def test_station_8_keyframes_plus_bev_replies_under_1s(env):
    client, _, _ = env
    _, files = station_bundle()  # 8 noisy 1280×720 JPEGs: worst case for thumbnailing
    client.post("/ingest/station", files=station_bundle(sid="s_0099")[1])  # warm up
    t0 = time.perf_counter()
    r = client.post("/ingest/station", files=files)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200
    assert elapsed < 1.0, f"{elapsed:.3f}s"


def test_keyframe_rows_positional_args(env):
    client, writer, _ = env
    client.post("/ingest/station", files=station_bundle()[1])
    kfs = writer.by("upsert_keyframe")
    assert [k[0] for k in kfs] == [f"k_{i:04d}" for i in range(1, 9)]
    kf_id, run_id, sid, t, x, y, hdg, thumb, url = kfs[2]
    assert (kf_id, run_id, sid, t, x, y, hdg) == (
        "k_0003", RUN, "s_0002", 1761300103.0, 0.8, 0.1, 120.0,
    )  # fmt: skip
    assert url == f"{PUBLIC}/files/runs/{RUN}/stations/s_0002/kf_002.jpg"
    assert len(thumb) <= 150_000
    assert _b64img(thumb).size == (320, 180)


def test_local_tile_row_positional_args(env):
    client, writer, _ = env
    client.post("/ingest/station", files=station_bundle()[1])
    (args,) = writer.by("upsert_local_tile")
    sid, run_id, t, x, y, hdg, res, w, h, png, url = args
    assert (sid, run_id, t, x, y, hdg, res, w, h) == (
        "s_0002", RUN, 1761300100.0, 0.8, 0.1, 30.0, 0.005, 360, 360,
    )  # fmt: skip
    assert url.endswith("/stations/s_0002/bev.png")
    img = _b64img(png)
    assert img.size == (360, 360) and img.mode == "RGBA"
    assert len(png) <= 700_000


def test_large_bev_shrinks_to_512_keeping_metric_size(env):
    client, writer, _ = env
    client.post("/ingest/station", files=station_bundle(bev_px=1024)[1])
    (args,) = writer.by("upsert_local_tile")
    res, w, h = args[6:9]
    assert (w, h) == (512, 512)
    assert res * w == pytest.approx(1024 * 0.005)


def test_station_logs_event_to_jsonl_and_stdb(env):
    client, writer, root = env
    client.post("/ingest/station", files=station_bundle()[1])
    (ev,) = _events(root)
    assert (ev["src"], ev["level"], ev["kind"]) == ("mapper", "info", "station_stored")
    run_id, t, src, level, kind, msg, data = writer.by("log_event")[0]
    assert (run_id, src, level, kind) == (RUN, "mapper", "info", "station_stored")
    assert json.loads(data)["station_id"] == "s_0002"


def test_missing_and_broken_files_warn_but_still_store(env):
    client, writer, root = env
    _, files = station_bundle(drop=("kf_003.jpg", "crop_p_0032.jpg"), bev_bytes=b"broken")
    r = client.post("/ingest/station", files=files)
    assert r.status_code == 200
    assert set(r.json()["urls"]["crops"]) == {"p_0031"}
    kfs = {k[0]: k for k in writer.by("upsert_keyframe")}
    assert len(kfs) == 8 and kfs["k_0004"][7] == ""  # row kept, no thumbnail
    assert writer.by("upsert_local_tile") == []
    (ev,) = _events(root)
    assert ev["level"] == "warn" and len(ev["data"]["warnings"]) == 3


def test_reupload_is_idempotent(env):
    client, writer, root = env
    files = station_bundle()[1]
    client.post("/ingest/station", files=files)
    r = client.post("/ingest/station", files=files)
    assert r.status_code == 200
    assert [e["data"]["reupload"] for e in _events(root)] == [False, True]
    assert len({k[0] for k in writer.by("upsert_keyframe")}) == 8


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(run_id="../evil"),
        lambda m: m.update(station_id="s_../x"),
        lambda m: m.pop("bev"),
        lambda m: m["pose"].update(source="gps"),
    ],
)
def test_station_rejects_bad_metadata(env, mutate):
    client, writer, root = env
    meta, files = station_bundle()
    mutate(meta)
    files[0] = ("station.json", ("station.json", json.dumps(meta), "application/json"))
    assert client.post("/ingest/station", files=files).status_code == 422
    assert writer.calls == []
    assert not (root / "runs").exists()


def test_station_rejects_missing_json_and_bad_part_names(env):
    client, _, _ = env
    _, files = station_bundle()
    assert client.post("/ingest/station", files=files[1:]).status_code == 422
    evil = files + [("../evil.jpg", ("../evil.jpg", b"x", "image/jpeg"))]
    assert client.post("/ingest/station", files=evil).status_code == 422
    assert client.post("/ingest/station", json={"a": 1}).status_code == 422


# --- /ingest/sample ------------------------------------------------------------------------


def _sample_files(meta=None, with_jpg=True):
    meta = meta or {
        "run_id": RUN,
        "sample_id": "a_0001",
        "cand_id": "c_0004",
        "t": 1761300200.0,
        "pose": {"x": 1.0, "y": 0.2, "heading_deg": 10.0, "source": "dr"},
    }
    files = [("sample.json", ("sample.json", json.dumps(meta), "application/json"))]
    if with_jpg:
        files.append(("closeup.jpg", ("closeup.jpg", noise_jpg(640, 480), "image/jpeg")))
    return files


def test_sample_upload(env):
    client, writer, root = env
    r = client.post("/ingest/sample", files=_sample_files())
    reply = SampleReply.model_validate(r.json())
    assert reply.url == f"{PUBLIC}/files/runs/{RUN}/samples/a_0001/closeup.jpg"
    assert (root / "runs" / RUN / "samples" / "a_0001" / "closeup.jpg").is_file()
    assert writer.by("log_event")[0][4] == "sample_stored"
    assert client.get(reply.url.removeprefix(PUBLIC)).status_code == 200


def test_sample_rejects_missing_photo_and_bad_ids(env):
    client, _, _ = env
    assert client.post("/ingest/sample", files=_sample_files(with_jpg=False)).status_code == 422
    bad = {"run_id": RUN, "sample_id": "../a", "cand_id": "c_1", "t": 1.0,
           "pose": {"x": 0, "y": 0, "heading_deg": 0, "source": "dr"}}  # fmt: skip
    assert client.post("/ingest/sample", files=_sample_files(bad)).status_code == 422


# --- /files, /fuse ---------------------------------------------------------------------------


def test_files_serves_uploads_and_blocks_traversal(env):
    client, _, _ = env
    client.post("/ingest/station", files=station_bundle(bev_bytes=bev_png())[1])
    r = client.get(f"/files/runs/{RUN}/stations/s_0002/bev.png")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    assert client.get("/files/..%2F..%2Fpyproject.toml").status_code == 404
    assert client.get("/files/runs/nope.png").status_code == 404


def test_module_app_exists_without_stdb_token(monkeypatch):
    from .. import app as app_module

    monkeypatch.delenv("STDB_TOKEN_MAPPER", raising=False)
    assert app_module.writer_from_env() is None
    monkeypatch.setenv("STDB_TOKEN_MAPPER", "tok")
    monkeypatch.setenv("MAPPER_NO_STDB", "1")
    assert app_module.writer_from_env() is None
    assert app_module.app is not None


def test_health_reports_stdb_writer(env):
    client, writer, _ = env
    assert client.get("/health").json() == {"ok": True, "stdb": True}
    writer.dropped = 1  # a write was dropped
    assert client.get("/health").json() == {"ok": True, "stdb": False}
