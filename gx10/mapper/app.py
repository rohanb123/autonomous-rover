"""Mapper service, FastAPI on :8200 (docs/00 §6.4-6.5, docs/roles/P3 §4-§6).

POST /ingest/station  multipart (00 §6.4) -> store, upsert_keyframe ×N, upsert_local_tile, log_event
POST /ingest/sample   multipart (00 §6.5) -> store present.jpg -> {ok, url}
POST /fuse/{run_id}   global map; not built yet (phase E) -> 501
GET  /files/{path}    files under the data root

Replies stay fast (< 1 s): thumbnails and the local tile are made synchronously but small, and
every STDB write goes through RateLimitedWriter's background thread, so a slow or down DB never
blocks the Pi. events.jsonl is the backup record when STDB is down.

Started by scripts/dev_up.sh as `uvicorn mapper.app:app` from gx10/ with config/.env loaded:
  MARSROVER_DATA_DIR  data root (default: repo data/, the /data symlink on the GX10)
  MAPPER_PUBLIC_URL   base of returned/stored URLs (default http://192.168.50.10:8200)
  STDB_URL, STDB_DB, STDB_TOKEN_MAPPER   SpacetimeDB writer (no token -> no DB writes, warns)
  MAPPER_NO_STDB=1    skip SpacetimeDB entirely
Or: `python -m mapper.app --help` from gx10/.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from PIL import UnidentifiedImageError
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from marsrover_shared import eventlog
from marsrover_shared.schemas import (
    MapperStationReply,
    SampleJson,
    SampleReply,
    StationJson,
    StationUrls,
)

from .store import RunStore, StoreError, check_name
from .tiles import bev_tile, thumb_b64

log = logging.getLogger("mapper")

CORS_ORIGINS = ["http://192.168.50.30:5173", "http://localhost:5173"]  # docs/00 §3
SRC = "mapper"


class Writer(Protocol):
    def submit(self, key: str, reducer: str, *args: Any) -> None: ...


# --- multipart -------------------------------------------------------------------------


def _part_name(field: str, upload: UploadFile) -> str:
    """Parts are named after their file (`kf_000.jpg`); fall back to the upload's filename."""
    return check_name(field if "." in field else Path(upload.filename or "").name)


async def read_bundle(request: Request, meta_part: str) -> tuple[dict, dict[str, bytes]]:
    """Split a multipart upload into its JSON metadata part and its file parts."""
    try:
        form = await request.form()
    except Exception as e:  # not multipart, or malformed
        raise HTTPException(422, f"expected multipart/form-data: {e}") from e
    meta_raw: str | bytes | None = None
    files: dict[str, bytes] = {}
    for field, value in form.multi_items():
        if isinstance(value, UploadFile):
            try:
                name = _part_name(field, value)
            except StoreError as e:
                raise HTTPException(422, str(e)) from e
            data = await value.read()
            if name == meta_part:
                meta_raw = data
            else:
                files[name] = data
        elif field == meta_part:
            meta_raw = value
    if meta_raw is None:
        raise HTTPException(422, f"missing {meta_part} part")
    try:
        meta = json.loads(meta_raw)
    except json.JSONDecodeError as e:
        raise HTTPException(422, f"{meta_part} is not JSON: {e}") from e
    if not isinstance(meta, dict):
        raise HTTPException(422, f"{meta_part} must be a JSON object")
    return meta, files


# --- app -------------------------------------------------------------------------------


def create_app(store: RunStore | None = None, writer: Writer | None = None) -> FastAPI:
    store = store or RunStore()
    app = FastAPI(title="mapper (P3)")
    app.add_middleware(
        CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"]
    )

    def db(reducer: str, *args: Any) -> None:
        if writer is not None:
            writer.submit(reducer, reducer, *args)  # no hz entry -> must-send FIFO, never dropped

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

    def event(run_id: str, level: str, kind: str, msg: str, data: dict[str, Any]) -> None:
        eventlog.log(run_id, SRC, level, kind, msg, data)
        db("log_event", run_id, time.time(), SRC, level, kind, msg, json.dumps(data))

    def process_station(st: StationJson, files: dict[str, bytes]) -> MapperStationReply:
        run_id, sid = st.run_id, st.station_id
        reupload = store.has_station(run_id, sid)
        rel = store.save_station(run_id, st.model_dump(mode="json"), files)
        warnings: list[str] = []
        x, y, hdg = st.pose.x, st.pose.y, st.pose.heading_deg

        for kf in st.keyframes:
            thumb = ""
            if kf.file not in files:
                warnings.append(f"missing keyframe file {kf.file}")
            else:
                try:
                    thumb = thumb_b64(files[kf.file])
                except (UnidentifiedImageError, OSError):
                    warnings.append(f"keyframe {kf.file} is not a readable image")
            # UNVERIFIED (docs/requests/2026-10-24-P3-to-P4.md item 2): kf.heading_deg is taken as
            # the absolute mission-frame heading at capture, as fake_brain writes it.
            db(
                "upsert_keyframe",
                kf.kf_id,
                run_id,
                sid,
                kf.t,
                x,
                y,
                kf.heading_deg,
                thumb,
                store.url_for(f"{rel}/{kf.file}"),
            )

        bev_url = store.url_for(f"{rel}/{st.bev.file}")
        if st.bev.file not in files:
            warnings.append(f"missing BEV file {st.bev.file}; no local tile written")
        else:
            try:
                tile = bev_tile(files[st.bev.file], st.bev.res_m_px)
            except (UnidentifiedImageError, OSError):
                warnings.append(f"BEV {st.bev.file} is not a readable image; no local tile")
            else:
                db(
                    "upsert_local_tile",
                    sid,
                    run_id,
                    st.t,
                    x,
                    y,
                    hdg,
                    tile.res_m_px,
                    tile.w_px,
                    tile.h_px,
                    tile.png_b64,
                    bev_url,
                )

        crops: dict[str, str] = {}
        for p in st.proposals:
            if p.crop in files:
                crops[p.prop_id] = store.url_for(f"{rel}/{p.crop}")
            else:
                warnings.append(f"missing crop {p.crop} for {p.prop_id}")

        event(
            run_id,
            "warn" if warnings else "info",
            "station_stored",
            f"{'re-stored' if reupload else 'stored'} {sid}: {len(st.keyframes)} keyframes, "
            f"{len(crops)}/{len(st.proposals)} crops"
            + (f"; {len(warnings)} warning(s)" if warnings else ""),
            {"station_id": sid, "reupload": reupload, "warnings": warnings},
        )
        return MapperStationReply(
            ok=True,
            station_id=sid,
            stored=f"/data/{rel}/",
            urls=StationUrls(bev=bev_url, crops=crops),
        )

    @app.post("/ingest/station", response_model=MapperStationReply)
    async def ingest_station(request: Request) -> MapperStationReply:
        meta, files = await read_bundle(request, "station.json")
        try:
            st = StationJson.model_validate(meta)
            RunStore.station_dir(st.run_id, st.station_id)  # validates both ids
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from e
        except StoreError as e:
            raise HTTPException(422, str(e)) from e
        return await run_in_threadpool(process_station, st, files)

    @app.post("/ingest/sample", response_model=SampleReply)
    async def ingest_sample(request: Request) -> SampleReply:
        meta, files = await read_bundle(request, "sample.json")
        try:
            sm = SampleJson.model_validate(meta)
            RunStore.sample_dir(sm.run_id, sm.sample_id)
        except ValidationError as e:
            raise HTTPException(422, e.errors(include_url=False, include_context=False)) from e
        except StoreError as e:
            raise HTTPException(422, str(e)) from e
        if "present.jpg" not in files:
            raise HTTPException(422, "missing present.jpg part")
        rel = await run_in_threadpool(
            store.save_sample, sm.run_id, sm.model_dump(mode="json"), files["present.jpg"]
        )
        event(
            sm.run_id,
            "info",
            "sample_stored",
            f"stored {sm.sample_id} for {sm.cand_id}",
            {"sample_id": sm.sample_id, "cand_id": sm.cand_id},
        )
        return SampleReply(ok=True, url=store.url_for(rel))

    @app.post("/fuse/{run_id}")
    def fuse(run_id: str) -> dict:
        raise HTTPException(501, "global map fusion is not built yet (P3 phase E)")

    @app.get("/files/{path:path}")
    def get_file(path: str) -> FileResponse:
        target = store.resolve(path)
        if target is None:
            raise HTTPException(404, "not found")
        return FileResponse(target)

    return app


# --- wiring from the environment (scripts/dev_up.sh) --------------------------------------


def writer_from_env() -> Writer | None:
    if os.environ.get("MAPPER_NO_STDB") == "1":
        return None
    if not os.environ.get("STDB_TOKEN_MAPPER"):
        log.warning("STDB_TOKEN_MAPPER is not set: mapper will not write to SpacetimeDB")
        return None
    from marsrover_shared.stdb_http import RateLimitedWriter, StdbClient

    def on_drop(reason: str, count: int) -> None:
        log.warning("stdb write dropped (%d so far): %s", count, reason)

    client = StdbClient.from_env(token_var="STDB_TOKEN_MAPPER")
    return RateLimitedWriter(client, hz={}, on_drop=on_drop)


def create_app_from_env() -> FastAPI:
    return create_app(RunStore(), writer_from_env())


app = create_app_from_env()


def main() -> None:
    ap = argparse.ArgumentParser(description="Mapper service (:8200)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8200)
    ap.add_argument("--public-url", default=None, help="default: $MAPPER_PUBLIC_URL or GX10")
    ap.add_argument("--data-root", type=Path, default=None, help="default: $MARSROVER_DATA_DIR")
    ap.add_argument("--no-stdb", action="store_true", help="don't write to SpacetimeDB")
    args = ap.parse_args()
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    writer = None if args.no_stdb else writer_from_env()
    uvicorn.run(
        create_app(RunStore(args.data_root, args.public_url), writer),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
