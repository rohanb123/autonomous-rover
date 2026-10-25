"""Pydantic models for every message in docs/00-overview-and-contracts.md §6.

Doc 00 is the source of truth: if a model here disagrees with it, fix the model.
Fields are required unless doc 00 marks them `?`. Unknown fields are rejected so
contract drift fails loudly instead of silently.
"""

from __future__ import annotations

import time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

Src = Literal["brain", "rover", "dashboard", "vision", "mapper", "overhead"]
Category = Literal["float_rock", "layered", "veined", "sand", "bedrock", "pebble", "any"]
PoseSource = Literal["dr", "dr+gyro", "vo", "aruco", "fused"]

_now = time.time


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- 6.3 Pose ---------------------------------------------------------------


class Pose(_Model):
    x: float
    y: float
    heading_deg: float
    source: PoseSource
    sigma_xy_m: float | None = None


# --- 6.1 Mission ------------------------------------------------------------


class Mission(_Model):
    mission_id: str
    run_id: str
    text: str
    radius_m: float = Field(ge=0.3, le=3.0)
    k: int = Field(ge=1, le=8)
    categories: list[Category] = Field(min_length=1)
    sample: bool
    return_home: bool
    max_duration_s: float = Field(gt=0)
    mode: Literal["act", "suggest"]
    assumptions: list[str] = Field(default_factory=list)


# --- 6.2 Brain <-> rover WebSocket --------------------------------------------

Op = Literal[
    "start_run",
    "drive",
    "turn",
    "stop",
    "estop",
    "clear_estop",
    "set_pose",
    "station",
    "approach",
    "sample",
    "snapshot",
    "heartbeat",
]
ErrCode = Literal[
    "E_ESTOP",
    "E_BUSY",
    "E_TIMEOUT",
    "E_BAD_ARGS",
    "E_TILT",
    "E_CAMERA",
    "E_LOST_TARGET",
    "E_UPLOAD",
    "E_INTERNAL",
]
RoverState = Literal["IDLE", "MOVING", "STATION", "APPROACH", "SAMPLING", "ESTOP"]


class NoArgs(_Model):
    pass


class StartRunArgs(_Model):
    run_id: str


class DriveArgs(_Model):
    dist_m: float
    speed: float = Field(ge=0.0, le=1.0)


class TurnArgs(_Model):
    deg: float
    speed: float = Field(ge=0.0, le=1.0)


class SetPoseArgs(_Model):
    x: float
    y: float
    heading_deg: float
    source: PoseSource


class StationArgs(_Model):
    station_id: str
    n_views: int = 8
    step_deg: float = 45.0


class ApproachArgs(_Model):
    cand_id: str
    x: float
    y: float


class SampleArgs(_Model):
    sample_id: str


CMD_ARGS: dict[str, type[_Model]] = {
    "start_run": StartRunArgs,
    "drive": DriveArgs,
    "turn": TurnArgs,
    "stop": NoArgs,
    "estop": NoArgs,
    "clear_estop": NoArgs,
    "set_pose": SetPoseArgs,
    "station": StationArgs,
    "approach": ApproachArgs,
    "sample": SampleArgs,
    "snapshot": NoArgs,
    "heartbeat": NoArgs,
}


class Cmd(_Model):
    type: Literal["cmd"] = "cmd"
    t: float = Field(default_factory=_now)
    src: Src = "brain"
    cmd_id: str
    op: Op
    args: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_args(self) -> Cmd:
        # Validate args against the op's table row; store them with defaults applied.
        self.args = CMD_ARGS[self.op].model_validate(self.args).model_dump()
        return self


class Ack(_Model):
    type: Literal["ack"] = "ack"
    t: float = Field(default_factory=_now)
    src: Src = "rover"
    cmd_id: str


class Done(_Model):
    type: Literal["done"] = "done"
    t: float = Field(default_factory=_now)
    src: Src = "rover"
    cmd_id: str
    result: dict[str, Any] = Field(default_factory=dict)


class Err(_Model):
    type: Literal["err"] = "err"
    t: float = Field(default_factory=_now)
    src: Src = "rover"
    cmd_id: str
    code: ErrCode
    msg: str = ""


class Imu(_Model):
    ax: float
    ay: float
    az: float
    gz_dps: float


class Telemetry(_Model):
    type: Literal["telemetry"] = "telemetry"
    t: float = Field(default_factory=_now)
    src: Src = "rover"
    run_id: str
    pose: Pose
    state: RoverState
    estop: bool
    imu: Imu
    # HC-SR04 forward range, metres. None when no sonar is attached or the last
    # echo timed out -- a missing reading and a zero reading mean opposite things.
    range_m: float | None = None
    cpu_temp_c: float
    queue_len: int = Field(ge=0)
    uplink_backlog: int = Field(ge=0)


class Event(_Model):
    """Ad-hoc rover event (6.2). Not to be confused with an events.jsonl line (6.13)."""

    type: Literal["event"] = "event"
    t: float = Field(default_factory=_now)
    src: Src = "rover"
    kind: Literal["tilt", "bump", "stall", "upload_retry", "camera_reset"]
    detail: dict[str, Any] = Field(default_factory=dict)


RoverMessage = Annotated[Cmd | Ack | Done | Err | Telemetry | Event, Field(discriminator="type")]
_rover_message = TypeAdapter(RoverMessage)


def parse_rover_message(raw: str | bytes) -> Cmd | Ack | Done | Err | Telemetry | Event:
    """Parse one WS frame from either side of the brain <-> rover socket."""
    return _rover_message.validate_json(raw)


# --- 6.4 / 6.5 Station and sample uploads (Pi -> mapper) ----------------------


class StationKeyframe(_Model):
    kf_id: str
    file: str
    heading_deg: float
    t: float


class Bev(_Model):
    file: str
    res_m_px: float = Field(gt=0)
    w_px: int = Field(gt=0)
    h_px: int = Field(gt=0)
    frame: Literal["rover@station"] = "rover@station"


class Proposal(_Model):
    prop_id: str
    kf_id: str
    bbox_px: list[int] = Field(min_length=4, max_length=4)
    crop: str
    xy: tuple[float, float]  # mission frame
    size_m: float
    contrast: float


class StationJson(_Model):
    run_id: str
    station_id: str
    t: float
    pose: Pose
    keyframes: list[StationKeyframe]
    bev: Bev
    proposals: list[Proposal]


class StationUrls(_Model):
    bev: str
    crops: dict[str, str] = Field(default_factory=dict)


class MapperStationReply(_Model):
    ok: bool
    station_id: str
    stored: str
    urls: StationUrls


class StationResult(StationJson):
    """`station` done.result: station.json plus the mapper's reply."""

    mapper: MapperStationReply


class SampleJson(_Model):
    run_id: str
    sample_id: str
    cand_id: str
    t: float
    pose: Pose


class SampleReply(_Model):
    ok: bool
    url: str


# --- 6.6 Vision /score ---------------------------------------------------------


class ScoreReqItem(_Model):
    prop_id: str
    crop_url: str
    size_m: float
    xy: tuple[float, float]


class ScoreReq(_Model):
    run_id: str
    items: list[ScoreReqItem]


class ScoreRespItem(_Model):
    prop_id: str
    visual: float
    novelty: float
    pickable: float
    category: Category
    category_probs: dict[str, float]
    terrain: Literal["soil", "bedrock", "sand", "big_rock"]
    embedding: list[float]  # L2-normalized; length = the model's, never assume it
    model: str


class ScoreResp(_Model):
    items: list[ScoreRespItem]


# --- 6.7 Vision /analyze ---------------------------------------------------------


class AnalyzeReq(_Model):
    run_id: str
    sample_id: str
    cand_id: str
    image_url: str
    interest_score: float = Field(ge=0.0, le=1.0)


class TrueModel(_Model):
    type: str
    amax: float
    k: float
    noise_sd: float


class CurveFit(_Model):
    type: str
    slope: float
    intercept: float
    r2: float
    fit_range: tuple[float, float]


class Standard(_Model):
    c: float
    a595: list[float]


class StandardCurveParams(_Model):
    standard: str
    wavelength_nm: float
    linear_range: tuple[float, float]
    true_model: TrueModel
    fit: CurveFit
    blank_a595: float
    standards: list[Standard]


class Assay(_Model):
    assay_type: Literal["bradford_sim"]
    simulated: Literal[True]
    model_version: str
    seed: int = Field(ge=0, lt=2**32)  # u32 so it survives JSON
    a595: float
    concentration: float
    units: str
    verdict: Literal["below_detection", "low", "elevated"]
    in_linear_range: bool
    standard_curve_params: StandardCurveParams


class AnalysisResult(_Model):
    sample_id: str
    label: str
    confidence: float
    caption: str
    simulated: Literal[True]
    assay: Assay | None = None  # omitted when the target is not in frame


# --- 6.8 - 6.10 Candidate, Task, Visit ---------------------------------------------


class Candidate(_Model):
    cand_id: str
    run_id: str
    station_id: str
    prop_ids: list[str]
    x: float
    y: float
    score: float
    novelty: float
    visual: float
    pickable: float
    category_score: float
    distance_m: float
    category: Category
    size_m: float
    status: Literal["new", "queued", "active", "visited", "skipped", "merged", "excluded", "failed"]
    rank: int  # -1 = not in top-k
    thumb_url: str
    reason: str = Field(max_length=140)


class Task(_Model):
    task_id: str
    run_id: str
    mission_id: str
    kind: Literal["station", "visit", "return"]
    ref: str
    seq: int = Field(ge=0)
    status: Literal["pending", "active", "done", "failed", "skipped"]
    note: str | None = None


class Visit(_Model):
    visit_id: str
    run_id: str
    cand_id: str
    x: float
    y: float
    radius_m: float = 0.20
    t: float
    outcome: Literal["sampled", "unreachable", "skipped"]


# --- 6.11 Dashboard -> brain REST --------------------------------------------------

Health = Literal["ok", "down", "degraded"]


class RunStartReq(_Model):
    notes: str | None = None


class MissionTextReq(_Model):
    text: str


class ApproveReq(_Model):
    mission_id: str
    approve: bool


class QuickReq(_Model):
    cmd: Literal["pause", "resume", "stop", "skip", "status", "come_back"]


class CommandAccepted(_Model):
    accepted: bool
    cmd_uid: str


class HealthResp(_Model):
    brain: Health
    rover_link: Health
    vision: Health
    mapper: Health
    stdb: Health
    overhead: Health


# --- 6.12 Overhead pose ----------------------------------------------------------


class OverheadPose(_Model):
    type: Literal["overhead_pose"] = "overhead_pose"
    t: float = Field(default_factory=_now)
    src: Src = "overhead"
    x: float
    y: float
    heading_deg: float
    marker_id: int
    reproj_err_px: float
    visible: bool


# --- 6.13 events.jsonl line ---------------------------------------------------------


class EventLogLine(_Model):
    t: float
    run_id: str
    src: Src
    level: Literal["info", "warn", "error"]
    kind: str
    msg: str
    data: dict[str, Any] = Field(default_factory=dict)
