"""Fake vision service (:8100) -- canned but deterministic answers on the real contract.

Owner: P3. Stands in for gx10/vision/app.py until the real /score lands (docs/04 §1).
Contract: docs/00 §6.6 (/score) and §6.7 (/analyze); models from marsrover_shared.schemas.

- /score returns seeded random *components* per (run_id, prop_id); the brain computes the score.
- /analyze calls the real SIMULATED Bradford simulator, so the assay is real from I0.
  `has_item` is seeded per sample (about 1 in 7 misses) so the brain's retry path runs.
- Nothing here fetches images or writes to SpacetimeDB (the brain writes analysis_record).

Run:  python mocks/fake_vision.py [--host 0.0.0.0] [--port 8100] [--empty-rate 0.15]
"""

from __future__ import annotations

import argparse
import hashlib
import math
import random
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "gx10", _ROOT / "shared" / "py"):  # same as scripts/_env.sh PYTHONPATH
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from marsrover_shared import eventlog  # noqa: E402
from marsrover_shared.schemas import (  # noqa: E402
    AnalysisResult,
    AnalyzeReq,
    ScoreReq,
    ScoreResp,
    ScoreRespItem,
)
from vision.bradford_sim import simulate  # noqa: E402

MODEL = "fake-vision-0"
HEADS = "fake-heads-0"
EMBED_DIM = 16
CATEGORIES = ["float_rock", "layered", "veined", "sand", "bedrock", "pebble"]
TERRAINS = ["soil", "bedrock", "sand", "big_rock"]
CORS_ORIGINS = ["http://192.168.50.30:5173", "http://localhost:5173"]  # docs/00 §3
CAPTIONS = {
    "float_rock": "A small loose rock fragment resting on the surface.",
    "layered": "A rock with visible parallel layering.",
    "veined": "A rock crossed by light-toned veins.",
    "sand": "Fine loose sand-like material.",
    "bedrock": "A flat exposed bedrock surface.",
    "pebble": "A small rounded pebble.",
}


def _rng(*parts: str) -> random.Random:
    digest = hashlib.sha256("/".join(parts).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _probs(rng: random.Random) -> dict[str, float]:
    w = [rng.gammavariate(0.7, 1.0) + 1e-6 for _ in CATEGORIES]
    total = sum(w)
    return {c: round(x / total, 4) for c, x in zip(CATEGORIES, w, strict=True)}


def _embedding(rng: random.Random) -> list[float]:
    v = [rng.gauss(0.0, 1.0) for _ in range(EMBED_DIM)]
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


def fake_item(run_id: str, prop_id: str) -> ScoreRespItem:
    rng = _rng("score", run_id, prop_id)
    probs = _probs(rng)
    return ScoreRespItem(
        prop_id=prop_id,
        visual=round(rng.uniform(0.05, 0.95), 3),
        novelty=round(rng.uniform(0.05, 1.0), 3),
        pickable=round(rng.uniform(0.0, 1.0), 3),
        category=max(probs, key=probs.get),
        category_probs=probs,
        terrain=rng.choice(TERRAINS),
        embedding=_embedding(rng),
        model=MODEL,
    )


def fake_analysis(req: AnalyzeReq, empty_rate: float) -> AnalysisResult:
    rng = _rng("analyze", req.run_id, req.sample_id)
    has_item = rng.random() >= empty_rate
    label = rng.choice(CATEGORIES)
    return AnalysisResult(
        sample_id=req.sample_id,
        label=label if has_item else "empty",
        confidence=round(rng.uniform(0.4, 0.9), 2) if has_item else 0.0,
        caption=CAPTIONS[label] if has_item else "The target is not in frame.",
        simulated=True,
        assay=simulate(req.sample_id, req.run_id, req.interest_score) if has_item else None,
    )


def create_app(empty_rate: float = 0.15, log_events: bool = True) -> FastAPI:
    app = FastAPI(title="fake_vision (P3 mock)")
    app.add_middleware(
        CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["*"], allow_headers=["*"]
    )

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "model": MODEL, "heads": HEADS}

    @app.post("/score", response_model=ScoreResp, response_model_exclude_none=True)
    def score(req: ScoreReq) -> ScoreResp:
        return ScoreResp(items=[fake_item(req.run_id, it.prop_id) for it in req.items])

    @app.post("/analyze", response_model=AnalysisResult, response_model_exclude_none=True)
    def analyze(req: AnalyzeReq) -> AnalysisResult:
        res = fake_analysis(req, empty_rate)
        if log_events:
            verdict = res.assay.verdict if res.assay else None
            eventlog.log(
                req.run_id,
                "vision",
                "info",
                "analysis",
                f"fake analyze {req.sample_id}: {res.label} verdict={verdict}",
                {"sample_id": req.sample_id, "cand_id": req.cand_id, "mock": True},
            )
        return res

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--empty-rate", type=float, default=0.15, help="share of missed targets")
    args = ap.parse_args()
    import uvicorn

    uvicorn.run(create_app(args.empty_rate), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
