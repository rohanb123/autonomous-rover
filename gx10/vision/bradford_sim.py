"""SIMULATED Bradford assay -- proof of concept. No wet lab. Deterministic per sample_id.

Reference implementation from docs/roles/P3-perception-ai.md §7 (algorithm ``bradford_sim-1``).
No real protein is measured; the numbers only look like a plausible Bradford readout.
Changing anything here that alters output is a contract change: bump MODEL_VERSION and update
the golden vector in docs/00 §6.7 (AGENTS.md §3).
"""

from __future__ import annotations

import hashlib
import math
import random

WAVELENGTH_NM = 595  # Thermo MAN0011203; Bio-Rad 4110065A
BSA_STANDARDS = [0, 25, 125, 250, 500, 750, 1000, 1500, 2000]  # µg/mL, Thermo Table 1
LINEAR_RANGE = (125.0, 1000.0)  # µg/mL, BSA, standard protocol (both vendors)
UNITS = "µg/mL"
# Illustrative "true" response used ONLY to synthesize numbers (ours, not a datasheet):
AMAX, K_HALF, NOISE_SD = 3.0, 3000.0, 0.01
REPLICATES = 3
MODEL_VERSION = "bradford_sim-1"


def seed_for(sample_id: str, run_id: str = "") -> int:
    """u32 seed: first 4 bytes (big-endian) of sha256("{run_id}/{sample_id}")."""
    return int.from_bytes(hashlib.sha256(f"{run_id}/{sample_id}".encode()).digest()[:4], "big")


def true_a595(c: float) -> float:
    return AMAX * c / (K_HALF + c)


def fit_linear(points: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Least-squares line through (x, y) points -> (slope, intercept, r2)."""
    n = len(points)
    sx = sum(p[0] for p in points)
    sy = sum(p[1] for p in points)
    sxx = sum(p[0] ** 2 for p in points)
    sxy = sum(p[0] * p[1] for p in points)
    m = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    b = (sy - m * sx) / n
    ybar = sy / n
    ss_tot = sum((y - ybar) ** 2 for _, y in points)
    ss_res = sum((y - (m * x + b)) ** 2 for x, y in points)
    return m, b, 1 - ss_res / ss_tot


def verdict_for(c: float) -> str:
    if c < LINEAR_RANGE[0]:
        return "below_detection"
    if c < 500.0:
        return "low"
    return "elevated"


def simulate(sample_id: str, run_id: str = "", interest_score: float | None = None) -> dict:
    """Return the SIMULATED assay dict of docs/00 §6.7 (``AnalysisResult.assay``)."""
    seed = seed_for(sample_id, run_id)
    rng = random.Random(seed)
    # 1) standards (blank-corrected, triplicates)
    raw = {
        c: [true_a595(c) + rng.gauss(0, NOISE_SD) for _ in range(REPLICATES)] for c in BSA_STANDARDS
    }
    blank = sum(raw[0]) / REPLICATES
    standards = [{"c": c, "a595": [round(a - blank, 4) for a in raw[c]]} for c in BSA_STANDARDS]
    pts = [
        (s["c"], a)
        for s in standards
        if LINEAR_RANGE[0] <= s["c"] <= LINEAR_RANGE[1]
        for a in s["a595"]
    ]
    m, b, r2 = fit_linear(pts)
    # 2) hidden "true" protein in the sample extract, biased by interest score (demo bias)
    bias = 0.5 if interest_score is None else min(max(interest_score, 0.0), 1.0)
    true_c = math.exp(rng.gauss(math.log(60.0 + 900.0 * bias), 0.45))
    a = max(true_a595(true_c) + rng.gauss(0, NOISE_SD) - blank, 0.0)  # blank-corrected
    # 3) read back through the fitted standard curve
    conc = max((a - b) / m, 0.0)
    return {
        "assay_type": "bradford_sim",
        "simulated": True,
        "model_version": MODEL_VERSION,
        "seed": seed,
        "a595": round(a, 4),
        "concentration": round(conc, 1),
        "units": UNITS,
        "verdict": verdict_for(conc),
        "in_linear_range": LINEAR_RANGE[0] <= conc <= LINEAR_RANGE[1],
        "standard_curve_params": {
            "standard": "BSA",
            "wavelength_nm": WAVELENGTH_NM,
            "linear_range": list(LINEAR_RANGE),
            "true_model": {"type": "hyperbolic", "amax": AMAX, "k": K_HALF, "noise_sd": NOISE_SD},
            "fit": {
                "type": "linear",
                "slope": round(m, 7),
                "intercept": round(b, 4),
                "r2": round(r2, 4),
                "fit_range": list(LINEAR_RANGE),
            },
            "blank_a595": round(blank, 4),
            "standards": standards,
        },
    }
