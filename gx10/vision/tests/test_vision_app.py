"""Real vision app with a stand-in embedder, synthetic heads and in-memory image fetches."""

from __future__ import annotations

import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from marsrover_shared.schemas import AnalysisResult, ScoreResp

from ..app import VisionModels, create_app
from ..bradford_sim import simulate
from ..embed import l2_normalize
from ..heads import Heads
from .test_heads import _bundle

RUN = "r_20261024_101500"


class FakeEmbedder:
    """8-d embedding from the image's mean colour, so identical images embed identically."""

    name = "Fake/w"

    def embed(self, images):
        rows = []
        for im in images:
            a = np.asarray(im.convert("RGB").resize((4, 4)), dtype=np.float32) / 255.0
            m = a.mean(axis=(0, 1))
            rows.append([m[0], m[1], m[2], 1.0, m[0] - m[1], m[1] - m[2], 0.5, m.sum()])
        return l2_normalize(np.array(rows, dtype=np.float32))


def _jpg(color) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (96, 96), color).save(buf, format="JPEG")
    return buf.getvalue()


IMAGES = {
    "http://m/red.jpg": _jpg((200, 40, 40)),
    "http://m/blue.jpg": _jpg((40, 40, 200)),
    "http://m/present.jpg": _jpg((120, 90, 60)),
    "http://m/bad.jpg": b"not an image",
}


async def fake_fetch(url: str) -> bytes:
    if url not in IMAGES:
        raise FileNotFoundError(url)
    return IMAGES[url]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MARSROVER_DATA_DIR", str(tmp_path))
    models = VisionModels(FakeEmbedder(), Heads(_bundle()))
    with TestClient(create_app(models, fake_fetch)) as c:
        yield c


def _item(pid, url, size=0.02):
    return {"prop_id": pid, "crop_url": url, "size_m": size, "xy": [1.0, 0.2]}


def test_health(client):
    assert client.get("/health").json() == {"ok": True, "model": "Fake/w", "heads": "test+lr-v1"}


def test_score_returns_contract_components(client):
    body = client.post(
        "/score",
        json={"run_id": RUN, "items": [_item("p_0001", "http://m/red.jpg", 0.02),
                                        _item("p_0002", "http://m/blue.jpg", 0.04)]},
    ).json()  # fmt: skip
    resp = ScoreResp.model_validate(body)
    a, b = resp.items
    assert (a.prop_id, b.prop_id) == ("p_0001", "p_0002")
    assert a.pickable == 1.0 and b.pickable == pytest.approx(1 / 3, abs=1e-3)
    assert a.novelty == 1.0 and 0.0 <= b.novelty < 1.0
    assert abs(sum(a.category_probs.values()) - 1) < 1e-3
    assert a.category == max(a.category_probs, key=a.category_probs.get)
    assert abs(np.linalg.norm(a.embedding) - 1) < 1e-4 and len(a.embedding) == 8
    assert a.model == "test+lr-v1"


def test_novelty_memory_is_per_run_and_persists_across_calls(client):
    req = {"run_id": RUN, "items": [_item("p_0001", "http://m/red.jpg")]}
    first = client.post("/score", json=req).json()["items"][0]["novelty"]
    again = client.post("/score", json=req).json()["items"][0]["novelty"]
    other = client.post("/score", json={**req, "run_id": "r_20261024_120000"}).json()
    assert first == 1.0 and again == pytest.approx(0.0, abs=1e-3)
    assert other["items"][0]["novelty"] == 1.0


def test_unfetchable_crops_are_left_out_and_logged(client, tmp_path):
    body = client.post(
        "/score",
        json={"run_id": RUN, "items": [_item("p_0001", "http://m/red.jpg"),
                                        _item("p_0002", "http://m/missing.jpg"),
                                        _item("p_0003", "http://m/bad.jpg"),
                                        _item("p_0004", "")]},
    ).json()  # fmt: skip
    assert [i["prop_id"] for i in body["items"]] == ["p_0001"]
    log = (tmp_path / "runs" / RUN / "events.jsonl").read_text(encoding="utf-8")
    assert "score_skipped" in log and "p_0004" in log


def test_score_with_nothing_fetchable_returns_empty(client):
    req = {"run_id": RUN, "items": [_item("p_0001", "http://m/missing.jpg")]}
    assert client.post("/score", json=req).json() == {"items": []}


def test_analyze_labels_and_attaches_simulated_assay(client):
    req = {"run_id": RUN, "sample_id": "a_0001", "cand_id": "c_0001",
           "image_url": "http://m/present.jpg", "interest_score": 0.63}  # fmt: skip
    body = client.post("/analyze", json=req).json()
    res = AnalysisResult.model_validate(body)
    assert res.simulated is True and res.assay is not None
    assert body["assay"] == simulate("a_0001", RUN, 0.63)  # golden vector path
    assert res.label in {"float_rock", "layered", "veined", "sand", "bedrock", "pebble"}
    assert 0 <= res.confidence <= 1 and res.caption


def test_analyze_unfetchable_image_is_502(client):
    req = {"run_id": RUN, "sample_id": "a_0002", "cand_id": "c_0002",
           "image_url": "http://m/missing.jpg", "interest_score": 0.5}  # fmt: skip
    assert client.post("/analyze", json=req).status_code == 502


def test_bad_requests_are_422(client):
    assert client.post("/score", json={"run_id": RUN}).status_code == 422
    assert client.post("/analyze", json={"run_id": RUN}).status_code == 422
