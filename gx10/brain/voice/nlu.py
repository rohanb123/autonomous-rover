"""Mission text → Mission (doc 00 §6.1).

Grok (xAI) with a strict JSON schema when online; a regex parser as the offline backup.
Either way the result is clamped to the doc 00 §6.1 ranges and validated with the Mission
model, and every defaulted or clamped field is listed in `assumptions`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from brain import settings
from marsrover_shared.schemas import Mission

log = logging.getLogger(__name__)

CATEGORIES = ["float_rock", "layered", "veined", "sand", "bedrock", "pebble", "any"]
RADIUS_RANGE, K_RANGE = (0.3, 3.0), (1, 8)
WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}


# --- shared post-processing ------------------------------------------------------------


def _finish(
    fields: dict[str, Any], text: str, run_id: str, mission_id: str, assumptions: list[str]
) -> dict:
    """Clamp to doc 00 §6.1 ranges, record clamps, validate. Returns a Mission dict."""
    r = float(fields["radius_m"])
    if not RADIUS_RANGE[0] <= r <= RADIUS_RANGE[1]:
        clamped = min(max(r, RADIUS_RANGE[0]), RADIUS_RANGE[1])
        assumptions.append(f"radius {r:g} m is out of range: using {clamped:g} m")
        fields["radius_m"] = clamped
    k = int(fields["k"])
    if not K_RANGE[0] <= k <= K_RANGE[1]:
        clamped = min(max(k, K_RANGE[0]), K_RANGE[1])
        assumptions.append(f"k={k} is out of range: using {clamped}")
        fields["k"] = clamped
    cats = [c for c in fields.get("categories") or [] if c in CATEGORIES] or ["any"]
    mission = Mission(
        mission_id=mission_id,
        run_id=run_id,
        text=text,
        radius_m=fields["radius_m"],
        k=fields["k"],
        categories=cats,
        sample=bool(fields["sample"]),
        return_home=bool(fields["return_home"]),
        max_duration_s=float(fields["max_duration_s"]),
        mode=fields["mode"],
        assumptions=assumptions,
    )
    return mission.model_dump()


# --- Grok ----------------------------------------------------------------------------------

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "radius_m": {"type": "number", "description": "survey radius in metres"},
        "k": {"type": "integer", "description": "how many rocks to investigate"},
        "categories": {"type": "array", "items": {"type": "string", "enum": CATEGORIES}},
        "sample": {"type": "boolean", "description": "photograph each rock close up, or only look"},
        "return_home": {"type": "boolean"},
        "max_duration_s": {"type": "number"},
        "mode": {"type": "string", "enum": ["act", "suggest"]},
        "assumptions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "one short line per field the speaker did not state",
        },
    },
    "required": [
        "radius_m",
        "k",
        "categories",
        "sample",
        "return_home",
        "max_duration_s",
        "mode",
        "assumptions",
    ],
}

_SYSTEM = """\
You turn a spoken instruction for a small autonomous science rover into mission parameters.
The rover surveys around its start point, picks the most interesting rocks, optionally
photographs them close up, and can return home.
Rules:
- radius_m: survey radius in metres, 0.3 to 3.0. "a meter and a half" = 1.5. cm -> metres.
- k: number of rocks to investigate, 1 to 8.
- categories: rock types the speaker asked for, from the enum. ["any"] if none were named.
  float_rock = loose rock lying on the surface; layered = visible layers; veined = veins;
  pebble = small stones.
- sample: false only if they said not to sample, or to only look/survey.
- mode: "suggest" if they want to approve the plan first, else "act".
- For every field the speaker did NOT state, use the default below and add one
  assumption line saying so.
Defaults: {defaults}"""


def _parse_grok(text: str) -> tuple[dict[str, Any], list[str]]:
    from openai import OpenAI  # imported lazily: only the online path needs it

    v = settings.load().voice
    key = settings.env("XAI_API_KEY")
    if not key:
        raise RuntimeError("XAI_API_KEY is not set")
    client = OpenAI(api_key=key, base_url=v.nlu_base_url, timeout=v.nlu_timeout_s, max_retries=0)
    defaults = json.dumps(settings.load().mission_defaults)
    resp = client.chat.completions.create(
        model=v.nlu_model,
        messages=[
            {"role": "system", "content": _SYSTEM.format(defaults=defaults)},
            {"role": "user", "content": text},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "mission", "strict": True, "schema": _SCHEMA},
        },
        temperature=0,
    )
    fields = json.loads(resp.choices[0].message.content)
    return fields, list(fields.pop("assumptions", []))


# --- regex (offline backup) ------------------------------------------------------------------

_NUM = r"(\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight)"
_UNIT_M = r"met(?:er|re)s?"


def _num(tok: str) -> float:
    return float(WORD_NUM.get(tok, tok))


def _radius(t: str) -> float | None:
    if m := re.search(rf"{_NUM}\s*(?:cm|centimet(?:er|re)s?)\b", t):
        return _num(m.group(1)) / 100
    if m := re.search(rf"\b(a|an|{_NUM[1:-1]})\s+{_UNIT_M}\s+and\s+a\s+half", t):
        return (1.0 if m.group(1) in ("a", "an") else _num(m.group(1))) + 0.5
    if re.search(rf"\bhalf\s+a\s+{_UNIT_M}", t):
        return 0.5
    if m := re.search(rf"{_NUM}\s*(?:m|{_UNIT_M})\b", t):
        return _num(m.group(1))
    if re.search(rf"\ba\s+{_UNIT_M}\b", t):
        return 1.0
    return None


def _k(t: str) -> int | None:
    if m := re.search(rf"\b(?:top|best)\s+{_NUM}\b", t):
        return int(_num(m.group(1)))
    words = re.findall(r"[a-z0-9.]+", t)
    targets = re.compile(r"^(rocks?|samples?|targets?|stones?|pebbles?|candidates?)$")
    for i, w in enumerate(words):
        if re.fullmatch(_NUM, w) and not (
            i + 1 < len(words) and re.fullmatch(rf"m|cm|{_UNIT_M}", words[i + 1])
        ):
            if any(targets.match(x) for x in words[i + 1 : i + 5]):
                return int(_num(w))
    return None


_CAT_WORDS = [
    (r"\bfloat(?:ing)?\s+rocks?\b|\bfloats\b", "float_rock"),
    (r"\blayer(?:ed|s)?\b", "layered"),
    (r"\bvein(?:ed|s|y)?\b", "veined"),
    (r"\bsand(?:y)?\b", "sand"),
    (r"\bbedrock\b", "bedrock"),
    (r"\bpebbles?\b", "pebble"),
]


def _parse_regex(text: str, defaults: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    t = text.lower()
    assumptions: list[str] = []
    fields: dict[str, Any] = {}

    radius = _radius(t)
    if radius is None:
        radius = defaults["radius_m"]
        assumptions.append(f"radius not stated: using {radius:g} m")
    fields["radius_m"] = radius

    k = _k(t)
    if k is None:
        k = defaults["k"]
        assumptions.append(f"number of rocks not stated: using {k}")
    fields["k"] = k

    cats = [c for pat, c in _CAT_WORDS if re.search(pat, t)]
    if not cats:
        assumptions.append("no rock type named: any")
    fields["categories"] = cats or ["any"]

    if re.search(
        r"\b(?:don'?t|do not|no|without|never)\s+(?:\w+\s+)?(?:sampl|scoop|collect|pick)", t
    ) or re.search(r"\b(?:just|only)\s+(?:survey|look)", t):
        fields["sample"] = False
    elif re.search(r"\b(?:sampl|scoop|collect|pick\s+(?:them\s+|it\s+)?up|grab)", t):
        fields["sample"] = True
    else:
        fields["sample"] = defaults["sample"]
        assumptions.append(f"sampling not stated: {'sample' if fields['sample'] else 'look only'}")

    if re.search(r"\b(?:don'?t|do not)\s+(?:come back|return)|\bstay\s+(?:out|there)\b", t):
        fields["return_home"] = False
    elif re.search(r"\b(?:come|go|head)\s+(?:back|home)\b|\breturn\b|\bback\s+home\b", t):
        fields["return_home"] = True
    else:
        fields["return_home"] = defaults["return_home"]
        assumptions.append(f"return not stated: {'come back' if fields['return_home'] else 'stay'}")

    if m := re.search(rf"{_NUM}\s*(?:min|mins|minutes)\b", t):
        fields["max_duration_s"] = _num(m.group(1)) * 60
    else:
        fields["max_duration_s"] = defaults["max_duration_s"]

    suggest = (
        r"\b(?:suggest|ask (?:me|first)|wait for (?:my )?approval|check with me|let me approve)\b"
    )
    fields["mode"] = "suggest" if re.search(suggest, t) else defaults.get("mode", "act")
    return fields, assumptions


# --- public API ------------------------------------------------------------------------------


def parse_mission_ex(
    text: str, run_id: str, defaults: Mapping[str, Any] | None = None, mission_id: str = "m_0000"
) -> tuple[dict, str]:
    """(Mission dict, source) where source is 'grok' or 'regex'."""
    defaults = {**settings.load().mission_defaults, **(defaults or {})}
    try:
        fields, assumptions = _parse_grok(text)
        return _finish(fields, text, run_id, mission_id, assumptions), "grok"
    except Exception as e:  # offline, timeout, bad key, invalid output: all take the backup
        log.warning("Grok mission parse failed (%s); using the regex parser", e)
    fields, assumptions = _parse_regex(text, defaults)
    return _finish(fields, text, run_id, mission_id, assumptions), "regex"


def parse_mission(
    text: str, run_id: str, defaults: Mapping[str, Any] | None = None, mission_id: str = "m_0000"
) -> dict:
    """Text → Mission dict (doc 00 §6.1) with assumptions. Grok first, regex backup."""
    return parse_mission_ex(text, run_id, defaults, mission_id)[0]


_QUICK = [
    (r"^(?:e-?stop|stop|halt|freeze)\b", "stop"),
    (r"^(?:pause|hold on|wait)\b", "pause"),
    (r"^(?:resume|continue|go on|carry on)\b", "resume"),
    (r"^skip\b", "skip"),
    (r"^(?:status|report|how(?:'s| is) it going)\b", "status"),
    (r"^(?:come back|return(?: home)?|go home|head home)\b", "come_back"),
]


def parse_quick(text: str) -> str | None:
    """'stop' / 'pause' / … if the whole utterance is a short quick command, else None."""
    t = re.sub(r"[^a-z' -]", "", text.lower()).strip()
    if not t or len(t.split()) > 4:
        return None
    for pat, cmd in _QUICK:
        if re.search(pat, t):
            return cmd
    return None
