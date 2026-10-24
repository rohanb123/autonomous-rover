"""Client for the vision service (doc 00 §6.6–6.7) and the category priors it relies on."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx

from brain import settings
from marsrover_shared.schemas import ScoreReq, ScoreReqItem, ScoreResp, ScoreRespItem

log = logging.getLogger(__name__)

PRIORS_PATH = settings.ROOT / "ml" / "models" / "category_priors.json"  # written by P3 (ml/)
CATEGORIES = ("float_rock", "layered", "veined", "sand", "bedrock", "pebble")


def load_priors(path: Path = PRIORS_PATH) -> dict[str, float]:
    """ChemCam/AEGIS category priors (docs/03). Uniform 1.0 until P3's file exists."""
    if path.exists():
        return {k: float(v) for k, v in json.loads(path.read_text()).items()}
    log.warning("no %s yet: using uniform category priors", path)
    return dict.fromkeys(CATEGORIES, 1.0)


class VisionClient:
    def __init__(self, base_url: str, timeout_s: float = 5.0) -> None:
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)

    async def score(self, run_id: str, items: list[ScoreReqItem]) -> list[ScoreRespItem]:
        req = ScoreReq(run_id=run_id, items=items)
        resp = await self._http.post(
            "/score", content=req.model_dump_json(), headers={"Content-Type": "application/json"}
        )
        resp.raise_for_status()
        return ScoreResp.model_validate_json(resp.content).items

    async def close(self) -> None:
        await self._http.aclose()
