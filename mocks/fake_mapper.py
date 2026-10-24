"""Fake mapper service (:8200) -- stores uploads, writes canned tiles to SpacetimeDB.

Owner: P3. Stands in for gx10/mapper/app.py until the real mapper lands (docs/04 §1).
Contract: docs/00 §6.4 (/ingest/station), §6.5 (/ingest/sample); reducers in docs/02 §5.

- Files go to <data root>/runs/<run_id>/... (data root = $MARSROVER_DATA_DIR, else repo data/,
  which is the /data symlink on the GX10) and are served back under /files/runs/...
- Keyframe thumbnails are real downscales of the uploaded JPEGs (canned image if undecodable).
- Local and global tiles are canned PNGs: a grid plus an arrow pointing image-up (= rover +x),
  so MapView rotation (docs/06 D10) can be checked by eye.
- STDB writes go through RateLimitedWriter (background thread), so replies never wait on the DB.
  The mapper writes with STDB_TOKEN_MAPPER from config/.env. --no-stdb skips the DB entirely.

Run:  python mocks/fake_mapper.py [--port 8200] [--public-url http://localhost:8200] [--no-stdb]
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Protocol

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "gx10", _ROOT / "shared" / "py"):  # same as scripts/_env.sh PYTHONPATH
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from starlette.datastructures import UploadFile  # noqa: E402

from marsrover_shared import eventlog  # noqa: E402
from marsrover_shared.schemas import (  # noqa: E402
    MapperStationReply,
    SampleJson,
    SampleReply,
    StationJson,
    StationUrls,
)

DEFAULT_PUBLIC_URL = "http://192.168.50.10:8200"  # docs/00 §3
CORS_ORIGINS = ["http://192.168.50.30:5173", "http://localhost:5173"]
THUMB_MAX_CHARS = 150_000  # upsert_keyframe rejects longer (docs/02 §5)
TILE_MAX_CHARS = 700_000  # upsert_local_tile rejects longer
TILE_MAX_PX = 512
GLOBAL_RES_M_PX = 0.005
GLOBAL_HALF_M = TILE_MAX_PX * GLOBAL_RES_M_PX / 2  # one canned tile centered on the home pad
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_SAFE_ID = re.compile(r"^[a-z]_[0-9_]{4,32}$")


class Writer(Protocol):
    def submit(self, key: str, reducer: str, *args: Any) -> None: ...


# --- images ------------------------------------------------------------------------


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def canned_tile(w_px: int, h_px: int, label: str) -> Image.Image:
    """Grid + arrow pointing image-up (rover +x; mission +x for global tiles)."""
    img = Image.new("RGB", (w_px, h_px), (122, 82, 52))
    d = ImageDraw.Draw(img)
    step = max(max(w_px, h_px) // 8, 1)
    for x in range(0, w_px, step):
        d.line([(x, 0), (x, h_px)], fill=(150, 105, 70))
    for y in range(0, h_px, step):
        d.line([(0, y), (w_px, y)], fill=(150, 105, 70))
    cx, cy = w_px // 2, h_px // 2
    d.line([(cx, cy + h_px // 4), (cx, h_px // 8)], fill=(240, 240, 240), width=max(2, w_px // 64))
    d.polygon(
        [(cx, h_px // 16), (cx - w_px // 16, h_px // 6), (cx + w_px // 16, h_px // 6)],
        fill=(240, 240, 240),
    )
    d.text((6, h_px - 16), f"FAKE {label}", fill=(255, 255, 255))
    return img


def png_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return b64(buf.getvalue())


def thumb_b64(jpg: bytes | None, max_px: int = 320) -> str:
    """Downscaled JPEG as base64, kept under the upsert_keyframe size limit."""
    try:
        img = Image.open(io.BytesIO(jpg or b"")).convert("RGB")
    except Exception:
        img = canned_tile(max_px, max_px * 3 // 4, "keyframe")
    img.thumbnail((max_px, max_px))
    while True:
        for quality in (70, 50, 30):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            out = b64(buf.getvalue())
            if len(out) <= THUMB_MAX_CHARS:
                return out
        img.thumbnail((img.width // 2, img.height // 2))


# --- request helpers ---------------------------------------------------------------


def _check_id(value: str, what: str) -> str:
    """IDs become path components, so only accept the AGENTS.md §4 shapes."""
    if not _SAFE_ID.match(value):
        raise HTTPException(422, f"bad {what}: {value!r}")
    return value


def _part_name(field: str, upload: UploadFile) -> str:
    name = field if "." in field else Path(upload.filename or "").name
    if not _SAFE_NAME.match(name):
        raise HTTPException(422, f"bad file part name: {name!r}")
    return name


async def _read_form(request: Request, json_part: str) -> tuple[dict, dict[str, bytes]]:
    """Split a multipart upload into the JSON metadata part and the file parts."""
    form = await request.form()
    meta_raw: str | bytes | None = None
    files: dict[str, bytes] = {}
    for field, value in form.multi_items():
        if isinstance(value, UploadFile):
            name = _part_name(field, value)
            data = await value.read()
            if name == json_part:
                meta_raw = data
            else:
                files[name] = data
        elif field == json_part:
            meta_raw = value
    if meta_raw is None:
        raise HTTPException(422, f"missing {json_part} part")
    try:
        return json.loads(meta_raw), files
    except json.JSONDecodeError as e:
        raise HTTPException(422, f"{json_part} is not JSON: {e}") from e


# --- app -----------------------------------------------------------------------------


def create_app(
    data_root: Path | None = None,
    public_url: str = DEFAULT_PUBLIC_URL,
    writer: Writer | None = None,
) -> FastAPI:
    root = Path(data_root) if data_root else eventlog.data_root()
    public_url = public_url.rstrip("/")
    fuse_versions: dict[str, int] = {}
    app = FastAPI(title="fake_mapper (P3 mock)")
    app.add_middleware(
        CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"]
    )

    def url_for(rel: str) -> str:
        return f"{public_url}/files/{rel}"

    def db(reducer: str, *args: Any) -> None:
        if writer is not None:
            writer.submit(reducer, reducer, *args)  # no hz entry -> must-send FIFO

    # GET /health (docs/00 §6.5): stdb is false with no writer, or for 30 s after a
    # dropped write (RateLimitedWriter.dropped grew).
    health_state = {"dropped": 0, "bad_at": 0.0}

    def stdb_ok() -> bool:
        if writer is None:
            return False
        dropped = getattr(writer, "dropped", 0)
        if dropped > health_state["dropped"]:
            health_state["dropped"], health_state["bad_at"] = dropped, time.time()
        return time.time() - health_state["bad_at"] > 30.0

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "stdb": stdb_ok()}

    def log(run_id: str, kind: str, msg: str, data: dict[str, Any]) -> None:
        data = {**data, "mock": True}
        eventlog.log(run_id, "mapper", "info", kind, msg, data)
        db("log_event", run_id, time.time(), "mapper", "info", kind, msg, json.dumps(data))

    @app.post("/ingest/station", response_model=MapperStationReply)
    async def ingest_station(request: Request) -> MapperStationReply:
        meta, files = await _read_form(request, "station.json")
        try:
            st = StationJson.model_validate(meta)
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False)) from e
        run_id = _check_id(st.run_id, "run_id")
        sid = _check_id(st.station_id, "station_id")
        rel_dir = f"runs/{run_id}/stations/{sid}"
        out = root / rel_dir
        out.mkdir(parents=True, exist_ok=True)
        (out / "station.json").write_text(st.model_dump_json(indent=2), encoding="utf-8")
        for name, data in files.items():
            (out / name).write_bytes(data)

        x, y, hdg = st.pose.x, st.pose.y, st.pose.heading_deg
        for kf in st.keyframes:
            # kf.heading_deg is the absolute mission-frame heading at capture (docs/00 §6.4).
            db(
                "upsert_keyframe",
                kf.kf_id,
                run_id,
                sid,
                kf.t,
                x,
                y,
                kf.heading_deg,
                thumb_b64(files.get(kf.file)),
                url_for(f"{rel_dir}/{kf.file}"),
            )

        w, h, res = st.bev.w_px, st.bev.h_px, st.bev.res_m_px
        scale = min(1.0, TILE_MAX_PX / max(w, h))
        tw, th = max(1, round(w * scale)), max(1, round(h * scale))
        tile = png_b64(canned_tile(tw, th, f"local {sid}"))
        if len(tile) <= TILE_MAX_CHARS:
            db(
                "upsert_local_tile",
                sid,
                run_id,
                st.t,
                x,
                y,
                hdg,
                res * w / tw,  # keep the tile's metric size if it was downscaled
                tw,
                th,
                tile,
                url_for(f"{rel_dir}/{st.bev.file}"),
            )

        crops = {p.prop_id: url_for(f"{rel_dir}/{p.crop}") for p in st.proposals}
        log(
            run_id,
            "station_stored",
            f"fake mapper stored {sid}: {len(st.keyframes)} keyframes, {len(crops)} crops",
            {"station_id": sid, "files": sorted(files)},
        )
        return MapperStationReply(
            ok=True,
            station_id=sid,
            stored=f"/data/{rel_dir}/",
            urls=StationUrls(bev=url_for(f"{rel_dir}/{st.bev.file}"), crops=crops),
        )

    @app.post("/ingest/sample", response_model=SampleReply)
    async def ingest_sample(request: Request) -> SampleReply:
        meta, files = await _read_form(request, "sample.json")
        try:
            sm = SampleJson.model_validate(meta)
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False)) from e
        if "present.jpg" not in files:
            raise HTTPException(422, "missing present.jpg part")
        run_id = _check_id(sm.run_id, "run_id")
        sample_id = _check_id(sm.sample_id, "sample_id")
        rel_dir = f"runs/{run_id}/samples/{sample_id}"
        out = root / rel_dir
        out.mkdir(parents=True, exist_ok=True)
        (out / "sample.json").write_text(sm.model_dump_json(indent=2), encoding="utf-8")
        (out / "present.jpg").write_bytes(files["present.jpg"])
        log(run_id, "sample_stored", f"fake mapper stored {sample_id}", {"sample_id": sample_id})
        return SampleReply(ok=True, url=url_for(f"{rel_dir}/present.jpg"))

    @app.post("/fuse/{run_id}")
    def fuse(run_id: str) -> dict:
        _check_id(run_id, "run_id")
        version = fuse_versions.get(run_id, 0) + 1
        fuse_versions[run_id] = version
        # docs/06 §6: (originX, originY) = mission xy of the top-left pixel corner (max x, max y).
        db(
            "upsert_global_tile",
            f"g_{run_id}_0_0",
            run_id,
            0,
            0,
            GLOBAL_HALF_M,
            GLOBAL_HALF_M,
            GLOBAL_RES_M_PX,
            TILE_MAX_PX,
            TILE_MAX_PX,
            png_b64(canned_tile(TILE_MAX_PX, TILE_MAX_PX, "global")),
            version,
        )
        db("set_run_fused", run_id, True)
        log(run_id, "fused", f"fake mapper fused {run_id} (v{version})", {"method": "canned"})
        return {"ok": True, "tiles": 1}

    @app.get("/files/{path:path}")
    def get_file(path: str) -> FileResponse:
        base = root.resolve()
        target = (base / path).resolve()
        if not target.is_relative_to(base) or not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(target)

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8200)
    ap.add_argument(
        "--public-url",
        default=os.environ.get("MAPPER_PUBLIC_URL", DEFAULT_PUBLIC_URL),
        help="base of the URLs written to STDB and returned to the Pi",
    )
    ap.add_argument("--data-root", type=Path, default=None, help="default: $MARSROVER_DATA_DIR")
    ap.add_argument("--no-stdb", action="store_true", help="don't write to SpacetimeDB")
    args = ap.parse_args()

    writer = None
    if not args.no_stdb:
        from marsrover_shared.stdb_http import RateLimitedWriter, StdbClient

        writer = RateLimitedWriter(StdbClient.from_env(token_var="STDB_TOKEN_MAPPER"), hz={})
    import uvicorn

    app = create_app(args.data_root, args.public_url, writer)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
