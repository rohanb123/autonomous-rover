"""Vision service, FastAPI on :8100 (docs/00 §6.6-6.7, docs/roles/P3 §4-§5).

POST /score    ScoreReq -> ScoreResp: score *components* per proposal (the brain computes score)
POST /analyze  AnalyzeReq -> AnalysisResult: label + caption + the SIMULATED Bradford assay
GET  /health   {"ok": true, "model": <embedding model>, "heads": <heads version>}

Started by scripts/dev_up.sh as `uvicorn vision.app:app` from gx10/. The embedding model comes
from config/models.yaml and the heads from ml/models/heads.joblib (or $VISION_HEADS_PATH); if
either is missing the service refuses to start rather than return made-up numbers. Get both
with `python ml/setup_models.py` or a bundle from `ml/package_models.py` (docs/03 §7).

/score fetches each crop from its mapper URL. A crop that can't be fetched or decoded is left
out of the reply (the brain matches results by prop_id) and logged as a warning.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any, Protocol

import httpx
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image
from starlette.concurrency import run_in_threadpool

from marsrover_shared import eventlog
from marsrover_shared.schemas import (
    AnalysisResult,
    AnalyzeReq,
    ScoreReq,
    ScoreResp,
    ScoreRespItem,
)

from .analyze import analyze
from .bradford_sim import simulate
from .embed import REPO, Embedder, load_model_config
from .heads import Heads
from .novelty import NoveltyStore
from .pickable import pickable

log = logging.getLogger("vision")

CORS_ORIGINS = ["http://192.168.50.30:5173", "http://localhost:5173"]  # docs/00 §3
SRC = "vision"
FETCH_TIMEOUT_S = 3.0
EMBED_DECIMALS = 6


class EmbedderLike(Protocol):
    name: str

    def embed(self, images: list[Image.Image]) -> np.ndarray: ...


Fetcher = Any  # async (url: str) -> bytes


class VisionModels:
    """Embedder + heads, used under one lock (one GPU model, called from worker threads)."""

    def __init__(self, embedder: EmbedderLike, heads: Heads):
        self.embedder, self.heads = embedder, heads
        self.lock = threading.Lock()

    @classmethod
    def from_config(cls) -> VisionModels:
        cfg = load_model_config()
        embedder = Embedder.from_config()
        heads_path = os.environ.get("VISION_HEADS_PATH") or REPO / cfg["heads_path"]
        heads = Heads.load(heads_path, expect_model=embedder.name)
        return cls(embedder, heads)

    def embed(self, images: list[Image.Image]) -> np.ndarray:
        with self.lock:
            return self.embedder.embed(images)


def _decode(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def create_app(models: VisionModels | None = None, fetch: Fetcher | None = None) -> FastAPI:
    state: dict[str, Any] = {"models": models, "fetch": fetch}
    novelty = NoveltyStore()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        client = None
        if state["models"] is None:
            log.info("loading embedding model and heads ...")
            state["models"] = await run_in_threadpool(VisionModels.from_config)
            m = state["models"]
            log.info("vision ready: %s, heads %s", m.embedder.name, m.heads.name)
        if state["fetch"] is None:
            client = httpx.AsyncClient(timeout=FETCH_TIMEOUT_S, follow_redirects=True)

            async def http_fetch(url: str) -> bytes:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.content

            state["fetch"] = http_fetch
        try:
            yield
        finally:
            if client is not None:
                await client.aclose()

    app = FastAPI(title="vision (P3)", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"]
    )

    async def fetch_image(url: str) -> Image.Image:
        if not url:
            raise ValueError("empty URL")
        return _decode(await state["fetch"](url))

    @app.get("/health")
    def health() -> dict:
        m: VisionModels = state["models"]
        return {"ok": True, "model": m.embedder.name, "heads": m.heads.name}

    @app.post("/score", response_model=ScoreResp)
    async def score(req: ScoreReq) -> ScoreResp:
        m: VisionModels = state["models"]
        results = await asyncio.gather(
            *(fetch_image(it.crop_url) for it in req.items), return_exceptions=True
        )
        ok = [
            (it, img)
            for it, img in zip(req.items, results, strict=True)
            if isinstance(img, Image.Image)
        ]
        skipped = {
            it.prop_id: f"{type(r).__name__}: {r}"
            for it, r in zip(req.items, results, strict=True)
            if not isinstance(r, Image.Image)
        }
        if skipped:
            eventlog.log(
                req.run_id, SRC, "warn", "score_skipped",
                f"{len(skipped)} crop(s) could not be fetched; left out of /score",
                {"skipped": skipped},
            )  # fmt: skip
        if not ok:
            return ScoreResp(items=[])

        def compute() -> list[ScoreRespItem]:
            embs = m.embed([img for _, img in ok])
            vis = m.heads.visual(embs)
            probs = m.heads.category_probs(embs)
            terrain = m.heads.terrain(embs)
            nov = novelty.score_and_add(req.run_id, embs)  # arrival order = request order
            return [
                ScoreRespItem(
                    prop_id=it.prop_id,
                    visual=round(float(vis[i]), 4),
                    novelty=round(float(nov[i]), 4),
                    pickable=round(pickable(it.size_m), 4),
                    category=max(probs[i], key=probs[i].get),
                    category_probs={k: round(v, 4) for k, v in probs[i].items()},
                    terrain=terrain[i],
                    embedding=np.round(embs[i], EMBED_DECIMALS).tolist(),
                    model=m.heads.name,
                )
                for i, (it, _) in enumerate(ok)
            ]

        return ScoreResp(items=await run_in_threadpool(compute))

    @app.post("/analyze", response_model=AnalysisResult, response_model_exclude_none=True)
    async def analyze_sample(req: AnalyzeReq) -> AnalysisResult:
        m: VisionModels = state["models"]
        try:
            img = await fetch_image(req.image_url)
        except Exception as e:
            raise HTTPException(502, f"could not fetch {req.image_url!r}: {e}") from e

        def compute() -> dict:
            emb = m.embed([img])
            return analyze(img, emb[0], m.heads)

        info = await run_in_threadpool(compute)
        # UNVERIFIED placeholder (P3 decision 2026-10-24, option A): the real scoop_check needs
        # the empty-scoop reference photo and the scoop ROI, which aren't specified yet
        # (docs/roles/P3 §4 scoop_check). Until then every scoop counts as holding an item.
        has_item = True
        res = AnalysisResult(
            sample_id=req.sample_id,
            scoop_has_item=has_item,
            label=info["label"],
            confidence=info["confidence"],
            caption=info["caption"],
            simulated=True,
            assay=simulate(req.sample_id, req.run_id, req.interest_score) if has_item else None,
        )
        eventlog.log(
            req.run_id, SRC, "info", "analysis",
            f"{req.sample_id}: {res.label} ({res.confidence:.2f}), assay "
            f"{res.assay.verdict if res.assay else 'none'} (scoop check: placeholder)",
            {"sample_id": req.sample_id, "cand_id": req.cand_id, "scoop_check": "placeholder"},
        )  # fmt: skip
        return res

    return app


app = create_app()


def main() -> None:
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(description="Vision service (:8100)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8100)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
