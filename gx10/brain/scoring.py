"""Final candidate score and its reason string (doc 00 §8).

Vision returns components; the brain owns the formula, the category weighting and the
explanation the dashboard shows.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping

from brain import settings
from marsrover_shared.schemas import Mission, Pose


def category_score(
    probs: Mapping[str, float], priors: Mapping[str, float], wanted: Collection[str]
) -> float:
    """Σ_c probs[c] · prior[c] · want[c]; want = 1 if requested (or 'any'), else 0.2."""
    want_other = settings.load().scoring.want_unrequested
    any_ok = "any" in wanted
    return sum(
        p * priors.get(c, 0.0) * (1.0 if any_ok or c in wanted else want_other)
        for c, p in probs.items()
    )


def final_score(
    c: Mapping, mission: Mission, priors: Mapping[str, float], pose: Pose
) -> tuple[float, dict]:
    """Score one /score item (doc 00 §6.6 fields + `xy`) from the rover's current pose.

    Returns (score, components). Components carry what `explain` and the dashboard need.
    """
    s = settings.load().scoring
    x, y = c["xy"]
    distance = math.hypot(x - pose.x, y - pose.y)
    dist_norm = min(distance / mission.radius_m, 1.0)
    cat = category_score(c["category_probs"], priors, mission.categories)
    score = (
        s.w_novelty * c["novelty"]
        + s.w_visual * c["visual"]
        + s.w_pickable * c["pickable"]
        + s.w_category * cat
        - s.w_dist * dist_norm
    )
    components = {
        "novelty": c["novelty"],
        "visual": c["visual"],
        "pickable": c["pickable"],
        "category_score": cat,
        "distance_m": distance,
        "dist_norm": dist_norm,
    }
    if "size_m" in c:
        components["size_m"] = c["size_m"]
    return score, components


def explain(components: Mapping, category: str, wanted: Collection[str], distance_m: float) -> str:
    """Top 2 positive components with values + category match + distance, ≤ 140 chars."""
    s = settings.load().scoring
    size = components.get("size_m")
    labeled = [
        (s.w_novelty * components["novelty"], f"Novel texture ({components['novelty']:.2f})"),
        (s.w_visual * components["visual"], f"visually interesting ({components['visual']:.2f})"),
        (
            s.w_pickable * components["pickable"],
            (
                f"pickable size {size * 100:.1f} cm"
                if size is not None
                else f"pickable ({components['pickable']:.2f})"
            ),
        ),
        (
            s.w_category * components["category_score"],
            f"category fit ({components['category_score']:.2f})",
        ),
    ]
    top = [text for weight, text in sorted(labeled, key=lambda t: -t[0]) if weight > 0][:2]
    if category in wanted:
        match = f"matches requested '{category}'"
    elif "any" in wanted:
        match = f"'{category}'"
    else:
        match = f"not a requested category ('{category}')"
    parts = top + [match]
    text = ", ".join(parts) + f"; {distance_m:.1f} m away."
    text = text[0].upper() + text[1:]
    return text if len(text) <= s.reason_max_chars else text[: s.reason_max_chars - 1] + "…"
