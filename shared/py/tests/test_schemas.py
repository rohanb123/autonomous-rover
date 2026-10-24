import json

import pytest
from pydantic import ValidationError

from marsrover_shared import schemas as s

# Examples copied from docs/00-overview-and-contracts.md §6.
MISSION = {
    "mission_id": "m_0001",
    "run_id": "r_20261024_101500",
    "text": "Survey within a meter and a half, find the three most interesting rocks, sample them, and come back.",  # noqa: E501
    "radius_m": 1.5,
    "k": 3,
    "categories": ["float_rock", "layered", "veined", "any"],
    "sample": True,
    "return_home": True,
    "max_duration_s": 900,
    "mode": "act",
}
TELEMETRY = {
    "type": "telemetry",
    "t": 1761300000.4,
    "src": "rover",
    "run_id": "r_20261024_101500",
    "pose": {"x": 0.42, "y": -0.10, "heading_deg": 31.5, "source": "dr+gyro", "sigma_xy_m": 0.05},
    "state": "IDLE",
    "estop": False,
    "imu": {"ax": 0.01, "ay": -0.02, "az": 9.79, "gz_dps": 0.4},
    "scoop": {"pose": "STOW", "us": [1500, 1500]},
    "cpu_temp_c": 55.2,
    "queue_len": 0,
    "uplink_backlog": 0,
}
STATION = {
    "run_id": "r_20261024_101500",
    "station_id": "s_0002",
    "t": 1761300100.0,
    "pose": {"x": 0.8, "y": 0.0, "heading_deg": 0.0, "source": "dr+gyro"},
    "keyframes": [{"kf_id": "k_0009", "file": "kf_000.jpg", "heading_deg": 0.0, "t": 1761300101.2}],
    "bev": {
        "file": "bev.png",
        "res_m_px": 0.005,
        "w_px": 360,
        "h_px": 360,
        "frame": "rover@station",
    },
    "proposals": [
        {
            "prop_id": "p_0031",
            "kf_id": "k_0010",
            "bbox_px": [312, 288, 40, 36],
            "crop": "crop_p_0031.jpg",
            "xy": [1.12, 0.21],
            "size_m": 0.028,
            "contrast": 0.41,
        }
    ],
}
# 6.7 golden vector (standards list truncated to the entries doc 00 prints in full).
ANALYSIS = {
    "sample_id": "a_0001",
    "scoop_has_item": True,
    "label": "float_rock",
    "confidence": 0.62,
    "caption": "A small angular dark rock fragment with a smooth face.",
    "simulated": True,
    "assay": {
        "assay_type": "bradford_sim",
        "simulated": True,
        "model_version": "bradford_sim-1",
        "seed": 3896889563,
        "a595": 0.2585,
        "concentration": 282.1,
        "units": "µg/mL",
        "verdict": "low",
        "in_linear_range": True,
        "standard_curve_params": {
            "standard": "BSA",
            "wavelength_nm": 595,
            "linear_range": [125.0, 1000.0],
            "true_model": {"type": "hyperbolic", "amax": 3.0, "k": 3000.0, "noise_sd": 0.01},
            "fit": {
                "type": "linear",
                "slope": 0.0007188,
                "intercept": 0.0557,
                "r2": 0.9947,
                "fit_range": [125.0, 1000.0],
            },
            "blank_a595": -0.0068,
            "standards": [
                {"c": 0, "a595": [0.0073, -0.0063, -0.001]},
                {"c": 25, "a595": [0.0368, 0.0404, 0.0364]},
                {"c": 125, "a595": [0.1281, 0.1487, 0.134]},
            ],
        },
    },
}
CANDIDATE = {
    "cand_id": "c_0007",
    "run_id": "r_20261024_101500",
    "station_id": "s_0002",
    "prop_ids": ["p_0031", "p_0044"],
    "x": 1.12,
    "y": 0.21,
    "score": 0.63,
    "novelty": 0.64,
    "visual": 0.71,
    "pickable": 0.9,
    "category_score": 0.55,
    "distance_m": 1.14,
    "category": "float_rock",
    "size_m": 0.028,
    "status": "queued",
    "rank": 2,
    "thumb_url": "http://x/t.jpg",
    "reason": "Novel texture (0.64), pickable size 2.8 cm, matches requested 'float_rock'; 1.1 m away.",  # noqa: E501
}


@pytest.mark.parametrize(
    "model, data",
    [
        (s.Mission, MISSION),
        (s.Telemetry, TELEMETRY),
        (s.StationJson, STATION),
        (s.AnalysisResult, ANALYSIS),
        (s.Candidate, CANDIDATE),
    ],
)
def test_doc00_examples_round_trip(model, data):
    obj = model.model_validate(data)
    again = model.model_validate_json(obj.model_dump_json())
    assert again == obj


def test_golden_assay_values_survive_json():
    a = s.AnalysisResult.model_validate_json(json.dumps(ANALYSIS)).assay
    assert (a.seed, a.a595, a.concentration) == (3896889563, 0.2585, 282.1)


@pytest.mark.parametrize(
    "patch",
    [
        {"radius_m": 5.0},
        {"k": 0},
        {"categories": ["granite"]},
        {"categories": []},
        {"mode": "auto"},
        {"surprise": 1},
    ],
)
def test_mission_rejects_out_of_contract(patch):
    with pytest.raises(ValidationError):
        s.Mission.model_validate({**MISSION, **patch})


def test_analysis_must_be_simulated():
    with pytest.raises(ValidationError):
        s.AnalysisResult.model_validate({**ANALYSIS, "simulated": False})
    bad = json.loads(json.dumps(ANALYSIS))
    bad["assay"]["seed"] = 2**32
    with pytest.raises(ValidationError):
        s.AnalysisResult.model_validate(bad)


def test_cmd_validates_args_per_op():
    c = s.Cmd(cmd_id="q_000123", op="drive", args={"dist_m": 0.30, "speed": 0.5})
    assert c.args == {"dist_m": 0.30, "speed": 0.5}
    station = s.Cmd(cmd_id="q_000124", op="station", args={"station_id": "s_0001"})
    assert station.args == {"station_id": "s_0001", "n_views": 8, "step_deg": 45.0}
    for op, args in [
        ("drive", {"dist_m": 0.3, "speed": 2}),
        ("turn", {"deg": 90}),
        ("scoop_pose", {"pose": "WAVE"}),
        ("stop", {"x": 1}),
    ]:
        with pytest.raises(ValidationError):
            s.Cmd(cmd_id="q_000125", op=op, args=args)
    with pytest.raises(ValidationError):
        s.Cmd(cmd_id="q_000126", op="fly", args={})


def test_parse_rover_message_dispatches_on_type():
    raw = '{"type":"cmd","t":1761300000.12,"src":"brain","cmd_id":"q_000123","op":"drive","args":{"dist_m":0.30,"speed":0.5}}'  # noqa: E501
    assert isinstance(s.parse_rover_message(raw), s.Cmd)
    assert isinstance(s.parse_rover_message('{"type":"ack","cmd_id":"q_000123"}'), s.Ack)
    err = s.parse_rover_message('{"type":"err","cmd_id":"q_1","code":"E_ESTOP","msg":"latched"}')
    assert isinstance(err, s.Err) and err.code == "E_ESTOP"
    assert isinstance(s.parse_rover_message(json.dumps(TELEMETRY)), s.Telemetry)
    ev = s.parse_rover_message('{"type":"event","kind":"tilt","detail":{"deg":31}}')
    assert isinstance(ev, s.Event)
    with pytest.raises(ValidationError):
        s.parse_rover_message('{"type":"hello"}')


def test_station_result_extends_station_json():
    reply = {
        "ok": True,
        "station_id": "s_0002",
        "stored": "/data/runs/r_x/stations/s_0002/",
        "urls": {"bev": "http://192.168.50.10:8200/files/b.png", "crops": {"p_0031": "http://c"}},
    }
    r = s.StationResult.model_validate({**STATION, "mapper": reply})
    assert r.mapper.urls.crops["p_0031"] == "http://c"


def test_reason_is_capped_at_140_chars():
    with pytest.raises(ValidationError):
        s.Candidate.model_validate({**CANDIDATE, "reason": "x" * 141})
