"""Placeholder analysis of a scooped sample: label, confidence, caption (docs/roles/P3 §4).

The label comes from the category head on the PRESENT photo. Captions are templates for now;
an Ollama vision model can replace them later (config/models.yaml ollama.caption_model, I5,
first item on the docs/05 cut list).
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from .heads import Heads

TEMPLATES = {
    "float_rock": "A loose rock fragment, likely float rock.",
    "layered": "A rock showing what may be layering.",
    "veined": "A rock with what may be light-toned veins.",
    "sand": "Fine loose material, likely sand.",
    "bedrock": "A fragment that looks like bedrock.",
    "pebble": "A small rounded stone, likely a pebble.",
}


def analyze(image: Image.Image | None, crop_emb: np.ndarray, heads: Heads) -> dict:
    """{label, confidence, caption} from one embedding of the PRESENT photo.

    `image` is unused until a captioning model is wired in (it will get the photo itself).
    """
    probs = heads.category_probs(np.atleast_2d(crop_emb))[0]
    label = max(probs, key=probs.get)
    conf = float(probs[label])
    caption = TEMPLATES[label]
    if conf < 0.5:
        caption += f" (low confidence, {conf:.0%})"
    return {"label": label, "confidence": round(conf, 3), "caption": caption}
