"""mocks/fake_vision.py speaks the docs/00 §6.6-6.7 contract."""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marsrover_shared.schemas import AnalysisResult, ScoreResp

from ..bradford_sim import simulate

_MOCK = Path(__file__).resolve().parents[3] / "mocks" / "fake_vision.py"
RUN = "r_20261024_101500"


def _load():
    spec = importlib.util.spec_from_file_location("fake_vision", _MOCK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MARSROVER_DATA_DIR", str(tmp_path))
    return TestClient(_load().create_app())


def _score(client, run_id=RUN, ids=("p_0001", "p_0002", "p_0003")):
    items = [
        {"prop_id": p, "crop_url": f"http://x/{p}.jpg", "size_m": 0.02, "xy": [1, 0]} for p in ids
    ]
    r = client.post("/score", json={"run_id": run_id, "items": items})
    assert r.status_code == 200
    return r.json()


def _analyze_req(i: int, score: float = 0.5) -> dict:
    return {
        "run_id": RUN,
        "sample_id": f"a_{i:04d}",
        "cand_id": f"c_{i:04d}",
        "image_url": "http://x/a.jpg",
        "interest_score": score,
    }


def test_health(client):
    body = client.get("/health").json()
    assert body == {"ok": True, "model": "fake-vision-0", "heads": "fake-heads-0"}


def test_score_matches_contract_and_is_deterministic(client):
    body = _score(client)
    resp = ScoreResp.model_validate(body)
    assert [it.prop_id for it in resp.items] == ["p_0001", "p_0002", "p_0003"]
    for it in resp.items:
        assert math.isclose(sum(x * x for x in it.embedding), 1.0, rel_tol=1e-9)
        assert math.isclose(sum(it.category_probs.values()), 1.0, abs_tol=1e-3)
        assert it.category == max(it.category_probs, key=it.category_probs.get)
        assert 0 <= it.visual <= 1 and 0 <= it.novelty <= 1 and 0 <= it.pickable <= 1
    assert _score(client) == body
    assert _score(client, run_id="r_20261024_120000") != body


def test_score_rejects_bad_request(client):
    assert client.post("/score", json={"run_id": RUN}).status_code == 422


def test_analyze_uses_real_bradford_sim(client):
    body = client.post("/analyze", json=_analyze_req(1, 0.63)).json()
    res = AnalysisResult.model_validate(body)
    assert res.simulated is True
    if res.scoop_has_item:
        assert body["assay"] == simulate("a_0001", RUN, 0.63)
        assert res.assay.seed == 3896889563
    else:
        assert "assay" not in body


def test_empty_scoops_omit_assay_and_both_paths_occur(client):
    seen = set()
    for i in range(1, 61):
        body = client.post("/analyze", json=_analyze_req(i)).json()
        AnalysisResult.model_validate(body)
        seen.add(body["scoop_has_item"])
        assert ("assay" in body) == body["scoop_has_item"]
    assert seen == {True, False}


def test_analyze_writes_events_jsonl(client, tmp_path):
    client.post("/analyze", json=_analyze_req(2, 0.2))
    text = (tmp_path / "runs" / RUN / "events.jsonl").read_text(encoding="utf-8")
    assert text.count('"src":"vision"') == 1
