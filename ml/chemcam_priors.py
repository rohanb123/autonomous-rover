"""ChemCam category priors: how often real scientists (and AEGIS) chose each rock type (docs/03 §3).

Input:  ml/data/chemcam/msl_ccam_obs.csv  (ml/download/get_chemcam_obs.py)
Output: ml/models/category_priors.json         {category: prior}, max = 1.0
          -> read by gx10/brain/vision_client.load_priors (flat dict of floats, nothing else)
        ml/models/category_priors_detail.json  shares, counts, mapping, unmapped types
          -> for the dashboard ("scientists picked float rocks X% of the time") and review

Method (docs/03 §3):
1. AEGIS rows: Target Type mentions "AEGIS", or the Target name starts with "aegis".
   Everything else is a human pick.
2. Each row's Target Type (and Comments, for veins) maps to one of our six categories by the
   ordered keyword rules below, or to EXCLUDED (sky, calibration, drill holes, mosaics, ...), or
   stays UNMAPPED (e.g. the generic "Rock").
3. Count unique targets per category, not shots: a target's category is its most common row
   category; human and AEGIS targets are counted separately.
4. prior[c] = 0.7·share_human[c] + 0.3·share_aegis[c], scaled so the largest prior is 1.0.

UNVERIFIED: the keyword rules are a first pass; docs/03 asks for a check by eye (see --report).

Usage:  python ml/chemcam_priors.py [--csv PATH] [--out DIR] [--report]
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
DEFAULT_CSV = ROOT / "data" / "chemcam" / "msl_ccam_obs.csv"
DEFAULT_OUT = ROOT / "models"

CATEGORIES = ("float_rock", "layered", "veined", "sand", "bedrock", "pebble")
EXCLUDED = "excluded"
UNMAPPED = "unmapped"
HUMAN_WEIGHT, AEGIS_WEIGHT = 0.7, 0.3
MAPPING_VERSION = "chemcam-priors-1"

# Ordered: the first rule whose pattern matches the lower-cased Target Type wins.
TYPE_RULES: list[tuple[str, str]] = [
    # Not rock targets at all.
    (r"\bsky\b|calibration|\bcal\b|cal target|\bc?cct\b|magnet|dark test|\btest\b|laser", EXCLUDED),
    (r"rmi (mosaic|observation)|long[- ]distance|distant|mosaic|landscape|horizon", EXCLUDED),
    (r"drill|tailing|dump pile|brush|\bdrt\b|scuff|wheel|rover|hardware|sieve|turret", EXCLUDED),
    (r"meteorite", EXCLUDED),
    # Rock types.
    (r"vein|fracture fill", "veined"),
    (r"\bsand\b|dune|ripple|\bdrift\b", "sand"),
    (r"pebble|\bsoil\b|regolith|granule|gravel", "pebble"),
    (r"\bfloat\b", "float_rock"),
    (r"outcrop|layer|laminat|stratif|cross[- ]?bed", "layered"),
    (r"bedrock", "bedrock"),
]
# Comments that turn an otherwise rock/bedrock/unmapped target into "veined".
VEIN_COMMENT = re.compile(r"\bvein|calcium sulfate|\bcaso4\b|light[- ]toned fill", re.IGNORECASE)
_TYPE_RULES = [(re.compile(p), c) for p, c in TYPE_RULES]


def classify_type(target_type: str) -> str:
    t = target_type.strip().lower()
    for pattern, cat in _TYPE_RULES:
        if pattern.search(t):
            return cat
    return UNMAPPED


def row_category(target_type: str, comments: str) -> str:
    cat = classify_type(target_type)
    if cat in (UNMAPPED, "bedrock") and VEIN_COMMENT.search(comments or ""):
        return "veined"
    return cat


def load(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, encoding="utf-8", encoding_errors="replace", low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    df["Target"] = df["Target"].astype(str).str.strip()
    df["Target Type"] = df["Target Type"].fillna("").astype(str).str.strip()
    df["Comments"] = df["Comments"].fillna("").astype(str)
    df["aegis"] = df["Target Type"].str.contains("aegis", case=False) | df[
        "Target"
    ].str.lower().str.startswith("aegis")
    df["category"] = [
        row_category(t, c) for t, c in zip(df["Target Type"], df["Comments"], strict=True)
    ]
    return df


def target_categories(df: pd.DataFrame) -> pd.DataFrame:
    """One row per (target, aegis): its most common row category (ties: CATEGORIES order)."""
    order = {c: i for i, c in enumerate((*CATEGORIES, UNMAPPED, EXCLUDED))}

    def pick(cats: pd.Series) -> str:
        counts = Counter(cats)
        return min(counts, key=lambda c: (-counts[c], order[c]))

    return df.groupby(["Target", "aegis"])["category"].agg(pick).reset_index()


def shares(counts: dict[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    return {c: (counts[c] / total if total else 0.0) for c in CATEGORIES}


def compute(df: pd.DataFrame) -> dict:
    tgt = target_categories(df)
    result: dict = {"mapping_version": MAPPING_VERSION, "rows": len(df)}
    for who, is_aegis in (("human", False), ("aegis", True)):
        sub = tgt[tgt["aegis"] == is_aegis]
        cats = Counter(sub["category"])
        counts = {c: int(cats.get(c, 0)) for c in CATEGORIES}
        result[f"targets_{who}"] = counts
        result[f"share_{who}"] = shares(counts)
        result[f"targets_{who}_unmapped"] = int(cats.get(UNMAPPED, 0))
        result[f"targets_{who}_excluded"] = int(cats.get(EXCLUDED, 0))
    raw = {
        c: HUMAN_WEIGHT * result["share_human"][c] + AEGIS_WEIGHT * result["share_aegis"][c]
        for c in CATEGORIES
    }
    top = max(raw.values()) or 1.0
    result["priors"] = {c: round(raw[c] / top, 4) for c in CATEGORIES}
    result["weights"] = {"human": HUMAN_WEIGHT, "aegis": AEGIS_WEIGHT}
    result["type_rules"] = TYPE_RULES
    result["vein_comment_pattern"] = VEIN_COMMENT.pattern
    return result


def report(df: pd.DataFrame, result: dict, top_n: int = 40) -> str:
    lines = [f"rows: {len(df)}  (AEGIS rows: {int(df['aegis'].sum())})", ""]
    lines.append(f"Top {top_n} Target Types -> category (rows):")
    vc = df.groupby("Target Type")["category"].agg(lambda s: Counter(s).most_common(1)[0][0])
    sizes = df["Target Type"].value_counts()
    for ttype, n in sizes.head(top_n).items():
        lines.append(f"  {n:6d}  {ttype[:45]:45s} -> {vc[ttype]}")
    lines.append("")
    lines.append("Largest UNMAPPED types (rows):")
    um = df[df["category"] == UNMAPPED]["Target Type"].value_counts().head(15)
    lines += [f"  {n:6d}  {t[:60]}" for t, n in um.items()]
    vein_by_comment = (df["category"] == "veined") & ~df["Target Type"].str.contains(
        "vein", case=False
    )
    lines.append(f"\nRows made 'veined' by Comments: {int(vein_by_comment.sum())}")
    lines.append("\nUnique targets per category:")
    lines.append(
        f"  {'category':12s} {'human':>7s} {'aegis':>7s}  {'share_h':>7s} {'share_a':>7s}  prior"
    )
    for c in CATEGORIES:
        lines.append(
            f"  {c:12s} {result['targets_human'][c]:7d} {result['targets_aegis'][c]:7d}  "
            f"{result['share_human'][c]:7.3f} {result['share_aegis'][c]:7.3f}  "
            f"{result['priors'][c]:.3f}"
        )
    for who in ("human", "aegis"):
        lines.append(
            f"  {who}: unmapped targets {result[f'targets_{who}_unmapped']}, "
            f"excluded targets {result[f'targets_{who}_excluded']}"
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Build ChemCam category priors (docs/03 §3).")
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--report", action="store_true", help="print the mapping review table")
    args = ap.parse_args()

    df = load(args.csv)
    result = compute(df)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "category_priors.json").write_text(
        json.dumps(result["priors"], indent=2), encoding="utf-8"
    )
    (args.out / "category_priors_detail.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    if args.report:
        print(report(df, result))
    print(f"\npriors: {result['priors']}")
    print(f"wrote {args.out / 'category_priors.json'} and category_priors_detail.json")


if __name__ == "__main__":
    main()
