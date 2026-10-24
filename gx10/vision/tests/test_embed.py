"""Embedder logic with a stand-in model (no weights download): batching, normalization, config."""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest
import torch
from PIL import Image

from .. import embed as embed_mod


class _FakeModel:
    def __init__(self):
        self.batches: list[int] = []

    def eval(self):
        return self

    def encode_image(self, x: torch.Tensor) -> torch.Tensor:
        self.batches.append(x.shape[0])
        # Feature = mean pixel value repeated, plus a constant, so norms differ per image.
        m = x.float().mean(dim=(1, 2, 3), keepdim=False)
        return torch.stack([m * 3 + 1, m, torch.ones_like(m) * 2], dim=1)


@pytest.fixture()
def fake_open_clip(monkeypatch):
    model = _FakeModel()
    calls = {}

    def create_model_and_transforms(name, pretrained=None, device="cpu"):
        calls.update(name=name, pretrained=pretrained, device=device)
        preprocess = lambda im: torch.from_numpy(  # noqa: E731
            np.asarray(im.resize((8, 8)), dtype=np.float32).transpose(2, 0, 1) / 255.0
        )
        return model, None, preprocess

    mod = types.SimpleNamespace(create_model_and_transforms=create_model_and_transforms)
    monkeypatch.setitem(sys.modules, "open_clip", mod)
    return model, calls


def test_embed_is_l2_normalized_and_batched(fake_open_clip):
    model, calls = fake_open_clip
    e = embed_mod.Embedder("Fake-B-16", "fakepre", device="cpu")
    imgs = [Image.new("L", (20, 30), v) for v in range(0, 250, 10)]  # 25 grayscale images
    out = e.embed(imgs, batch_size=8)
    assert out.shape == (25, 3) and out.dtype == np.float32
    assert np.allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-6)
    assert model.batches == [8, 8, 8, 1]
    assert calls == {"name": "Fake-B-16", "pretrained": "fakepre", "device": "cpu"}
    assert e.name == "Fake-B-16/fakepre"


def test_cuda_falls_back_to_cpu_when_unavailable(fake_open_clip, monkeypatch):
    _, calls = fake_open_clip
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    e = embed_mod.Embedder("Fake", "p", device="cuda")
    assert e.device == "cpu" and calls["device"] == "cpu"


def test_empty_input(fake_open_clip):
    assert embed_mod.Embedder("Fake", "p", device="cpu").embed([]).shape[0] == 0


def test_from_config_reads_models_yaml(fake_open_clip, tmp_path):
    _, calls = fake_open_clip
    cfg = tmp_path / "models.yaml"
    cfg.write_text(
        "embedding:\n  open_clip_model: Fake-X\n  pretrained: w\n  device: cpu\n", encoding="utf-8"
    )
    e = embed_mod.Embedder.from_config(cfg)
    assert (calls["name"], calls["pretrained"], e.device) == ("Fake-X", "w", "cpu")
    cfg.write_text("embedding:\n  open_clip_model: ''\n  pretrained: ''\n", encoding="utf-8")
    with pytest.raises(ValueError):
        embed_mod.Embedder.from_config(cfg)


def test_repo_config_names_a_model():
    emb = embed_mod.load_model_config()["embedding"]
    assert emb["open_clip_model"] and emb["pretrained"]


def test_l2_normalize_handles_zero_rows():
    out = embed_mod.l2_normalize(np.array([[3.0, 4.0], [0.0, 0.0]]))
    assert np.allclose(out[0], [0.6, 0.8]) and np.allclose(out[1], [0.0, 0.0])
