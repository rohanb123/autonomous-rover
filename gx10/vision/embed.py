"""Image embeddings with a frozen open_clip model (docs/03 §1, docs/roles/P3 §4).

The same class is used offline by ml/build_embeddings.py and online by the vision service, so
training and serving always see identical preprocessing. The model is never trained here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

REPO = Path(__file__).resolve().parents[2]
MODELS_YAML = REPO / "config" / "models.yaml"


def load_model_config(path: Path = MODELS_YAML) -> dict[str, Any]:
    import yaml

    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class Embedder:
    def __init__(self, model_name: str, pretrained: str, device: str = "cuda"):
        import open_clip
        import torch

        if device.startswith("cuda") and not torch.cuda.is_available():
            device = "cpu"
        self.device = device
        self.model_name, self.pretrained = model_name, pretrained
        model, _, preprocess = open_clip.create_model_and_transforms(
            model_name, pretrained=pretrained, device=device
        )
        model.eval()
        self._model, self._preprocess, self._torch = model, preprocess, torch

    @classmethod
    def from_config(cls, path: Path = MODELS_YAML) -> Embedder:
        emb = load_model_config(path)["embedding"]
        if not emb.get("open_clip_model"):
            raise ValueError(f"no embedding model set in {path}")
        return cls(emb["open_clip_model"], emb["pretrained"], emb.get("device", "cuda"))

    @property
    def name(self) -> str:
        return f"{self.model_name}/{self.pretrained}"

    def embed(self, images: list[Image.Image], batch_size: int = 64) -> np.ndarray:
        """(n, d) float32, each row L2-normalized. Images may be any mode/size."""
        torch = self._torch
        out: list[np.ndarray] = []
        use_amp = self.device.startswith("cuda")
        with torch.no_grad():
            for i in range(0, len(images), batch_size):
                batch = torch.stack(
                    [self._preprocess(im.convert("RGB")) for im in images[i : i + batch_size]]
                ).to(self.device)
                with torch.autocast("cuda", enabled=use_amp):
                    feats = self._model.encode_image(batch)
                out.append(feats.float().cpu().numpy())
        if not out:
            return np.zeros((0, 0), dtype=np.float32)
        return l2_normalize(np.concatenate(out, axis=0))


def l2_normalize(x: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(x, axis=1, keepdims=True)
    return (x / np.maximum(norms, 1e-12)).astype(np.float32)
