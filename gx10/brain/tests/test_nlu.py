import os

import pytest

from brain.voice import nlu

DEFAULTS = {
    "radius_m": 1.5,
    "k": 3,
    "categories": ["any"],
    "sample": True,
    "return_home": True,
    "max_duration_s": 900,
    "mode": "act",
}
SPEC = "Survey within a meter and a half, find the three most interesting rocks, sample them, and come back."  # noqa: E501


@pytest.fixture
def offline(monkeypatch):
    """Force the regex path, as if Grok were unreachable."""

    def boom(text):
        raise ConnectionError("no internet")

    monkeypatch.setattr(nlu, "_parse_grok", boom)


def test_spec_example_offline(offline):
    m, source = nlu.parse_mission_ex(SPEC, "r_1", DEFAULTS, "m_0001")
    assert source == "regex"
    assert (m["radius_m"], m["k"], m["sample"], m["return_home"], m["mode"]) == (
        1.5,
        3,
        True,
        True,
        "act",
    )
    assert m["categories"] == ["any"] and m["assumptions"] == ["no rock type named: any"]
    assert (m["mission_id"], m["run_id"], m["text"]) == ("m_0001", "r_1", SPEC)


@pytest.mark.parametrize(
    "text, want",
    [
        (
            "Look within 80 cm for 2 layered rocks and scoop them",
            {"radius_m": 0.8, "k": 2, "categories": ["layered"]},
        ),
        (
            "Find the top 5 float rocks or veined ones within 2 meters",
            {"radius_m": 2.0, "k": 5, "categories": ["float_rock", "veined"]},
        ),
        ("Survey half a meter, don't sample anything", {"radius_m": 0.5, "sample": False}),
        (
            "just survey the pebbles and stay out there",
            {"sample": False, "return_home": False, "categories": ["pebble"]},
        ),
        (
            "find four rocks within 1.2 m, suggest a plan and wait for my approval",
            {"k": 4, "radius_m": 1.2, "mode": "suggest"},
        ),
        ("explore for 10 minutes", {"max_duration_s": 600.0}),
    ],
)
def test_regex_phrasings(offline, text, want):
    m = nlu.parse_mission(text, "r_1", DEFAULTS)
    assert {k: m[k] for k in want} == want


def test_out_of_range_values_are_clamped_and_noted(offline):
    m = nlu.parse_mission("search 5 meters for 12 rocks", "r_1", DEFAULTS)
    assert (m["radius_m"], m["k"]) == (3.0, 8)
    assert any("radius 5" in a for a in m["assumptions"]) and any(
        "k=12" in a for a in m["assumptions"]
    )


def test_unstated_fields_use_defaults_with_assumptions(offline):
    m = nlu.parse_mission("go look at some bedrock", "r_1", DEFAULTS)
    assert (m["radius_m"], m["k"], m["categories"]) == (1.5, 3, ["bedrock"])
    assert len(m["assumptions"]) == 4  # radius, k, sampling, return


def test_grok_result_is_clamped_and_validated(monkeypatch):
    fields = {
        "radius_m": 9.0,
        "k": 3,
        "categories": ["float_rock", "granite"],
        "sample": True,
        "return_home": True,
        "max_duration_s": 900,
        "mode": "act",
    }
    monkeypatch.setattr(nlu, "_parse_grok", lambda text: (dict(fields), ["k defaulted"]))
    m, source = nlu.parse_mission_ex("whatever", "r_1", DEFAULTS)
    assert source == "grok"
    assert m["radius_m"] == 3.0 and m["categories"] == ["float_rock"]
    assert m["assumptions"][0] == "k defaulted"


def test_invalid_grok_output_falls_back_to_regex(monkeypatch):
    monkeypatch.setattr(nlu, "_parse_grok", lambda text: ({"radius_m": 1.0}, []))  # missing fields
    assert nlu.parse_mission_ex(SPEC, "r_1", DEFAULTS)[1] == "regex"


@pytest.mark.parametrize(
    "text, cmd",
    [
        ("Stop!", "stop"),
        ("pause", "pause"),
        ("ok continue", None),
        ("continue", "resume"),
        ("come back", "come_back"),
        ("skip this one", "skip"),
        ("status", "status"),
        (SPEC, None),
    ],
)
def test_parse_quick(text, cmd):
    assert nlu.parse_quick(text) == cmd


@pytest.mark.skipif(
    os.environ.get("XAI_LIVE") != "1", reason="set XAI_LIVE=1 to call the real Grok API"
)
def test_live_grok_parses_spec_example():
    m, source = nlu.parse_mission_ex(SPEC, "r_1", DEFAULTS)
    assert source == "grok"
    assert (m["radius_m"], m["k"], m["sample"], m["return_home"]) == (1.5, 3, True, True)
