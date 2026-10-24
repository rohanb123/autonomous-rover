# P4: Brain, data & dashboard lead (SW/backend lead)

## 1. Goal
Own the **decision loop** and the **single source of truth**: the brain service that turns a mission into a mini science campaign (survey → score → top-k → scoop → log), the SpacetimeDB module, the dashboard app shell and panels, the shared contracts package, and integration. You are also the **integration lead**: you run the milestones in [04](../04-integration-timeline.md) and approve contract changes.

## 2. Scope
| Yours | Not yours |
|---|---|
| `gx10/brain/**`, including `voice/` (ASR + NLU) | Vision/ML models and `config/models.yaml` (P3) |
| `spacetimedb/**` (schema + reducers), `scripts/**`, `config/**` except `models.yaml` (P3), `shared/py/**`, `Makefile` and root tooling files | Map canvas + layers, BradfordPanel (P3); you pass them rows as props |
| `dashboard/**` except `src/map/` and `src/assay/` | Rover primitives (P2), scoring components (P3) |
| `mocks/fake_brain.py`, `mocks/replay_run.py` | Hardware (P1) |
| README, AGENTS, CLAUDE.md, docs 00/02/04/05/06/sources/TEAM | docs 01 (P1), 03 (P3), other role files |

## 3. Hardware / environment
GX10 at 192.168.50.10 (Ethernet), travel router `marsnet`, dashboard laptop .30. SpacetimeDB **v2.13.0 standalone** (ARM64 build exists) on :3000; Node 20+ for the module and dashboard; Python 3.11+ (`fastapi`, `uvicorn`, `websockets`, `httpx`, `pydantic` v2, `numpy`, `faster-whisper` ⚠️ ARM64/CUDA wheel, CPU `int8` fallback, `openai` SDK for Grok). Internet at the venue for Grok voice; the offline backup needs none. No physical parts.

## 4. Files and function signatures
```python
# shared/py/marsrover_shared/  (everyone imports this; you own it)
schemas.py:  class Mission, Cmd, Ack, Done, Err, Telemetry, Pose, StationJson, SampleJson, ScoreReq/Resp, AnalyzeReq, AnalysisResult, Assay, Candidate, Task, Visit, OverheadPose, Event (pydantic)
ids.py:      def new_run_id(now: datetime) -> str; class IdGen: def next(self, prefix: str) -> str   # "c_0007"
frames.py:   def wrap_deg(a: float) -> float; def rover_to_mission(pose: Pose, xy_r) -> tuple; def mission_to_rover(pose: Pose, xy_m) -> tuple
stdb_http.py: class StdbClient; class RateLimitedWriter   (02 §8)
eventlog.py: def log(run_id: str, src: str, level: str, kind: str, msg: str, data: dict | None = None) -> None   # data/runs/<run>/events.jsonl

# gx10/brain/voice/asr.py
def transcribe(audio: bytes, mime: str = "audio/webm") -> str        # Grok STT (POST /v1/stt, keyterms); offline: ffmpeg → 16 kHz wav → faster-whisper

# gx10/brain/voice/nlu.py
def parse_mission(text: str, run_id: str, defaults: dict) -> dict     # → Mission (00 §6.1) + assumptions; Grok structured output, regex fallback
def parse_quick(text: str) -> str | None                              # "stop"/"pause"/… if the utterance is a quick command
# Both modules also expose *_ex variants returning (result, source), source = "grok" | "regex" | "faster-whisper".
# parse_mission takes an optional mission_id; asr raises AsrUnavailable when neither path works.

# gx10/brain/scoring.py
def final_score(c: dict, mission: Mission, priors: dict[str, float], pose: Pose) -> tuple[float, dict]   # (score, components)
def category_score(probs: dict[str,float], priors: dict[str,float], wanted: set[str]) -> float
def explain(components: dict, category: str, wanted: set[str], distance_m: float) -> str             # ≤ 140 chars

# gx10/brain/dedupe.py
def merge_or_new(prop: dict, cands: dict[str, Candidate], embeddings: dict[str, list[float]]) -> str | None  # existing cand_id or None; thresholds from run.yaml
def merge(cand: Candidate, prop_id: str, new: Candidate) -> Candidate   # keeps max score, appends prop_id

# gx10/brain/visited.py
def excluded(xy: tuple[float,float], visits: list[Visit], radius_m: float | None = None) -> bool   # default: each visit's radius_m (0.20)
def newly_excluded(cands: dict[str, Candidate], visits: list[Visit]) -> list[str]

# gx10/brain/planner.py
def station_plan(radius_m: float) -> list[tuple[float,float]]          # home; + 4 points at 0.6·radius if radius > 0.8
class TopK:
    def __init__(self, k: int): ...
    def offer(self, cand_id: str, score: float) -> None                   # heapq min-heap capped at k
    def rebuild(self, cands: dict[str, Candidate]) -> None                # after merge/skip/exclusion
    def items(self) -> list[tuple[float, str]]                            # sorted desc → ranks
def nn_route(start: tuple[float,float], targets: list[Candidate]) -> list[Candidate]

# gx10/brain/fsm.py
class RunFSM:   # states: IDLE SURVEY PLAN AWAIT_APPROVAL INVESTIGATE RETURN DONE PAUSED STOPPED ESTOP (00 §7)
    async def start_mission(self, mission: Mission) -> None
    async def on_quick(self, cmd: str) -> None
    async def on_estop(self) -> None; async def on_clear_estop(self) -> None
    async def investigate(self, cand: Candidate) -> Visit                 # approach → scoop → analyze → dump → mark_visited

# gx10/brain/rover_link.py
class RoverLink:
    async def connect(self, url: str = "ws://192.168.50.20:8765/rover") -> None   # auto-reconnect, heartbeat 0.25 s
    async def cmd(self, op: str, args: dict, timeout_s: float) -> dict           # ack ≤ 0.5 s, then done/err → raises RoverError(code)
    async def estop(self) -> None                                                 # bypasses the queue
    on_telemetry: Callable[[Telemetry], None]; on_event: Callable[[dict], None]

# gx10/brain/overhead_link.py
class OverheadLink:
    async def run(self, url: str = "ws://192.168.50.10:8300/pose") -> None
    def latest(self, max_age_s: float = 0.5) -> OverheadPose | None   # brain sends set_pose(source="aruco") at each station if fresh and reproj_err_px < 2

# gx10/brain/stdb_writer.py
class BrainDb:   # thin wrappers that call RateLimitedWriter / StdbClient with positional args from 02 §6
    def run_started(...); def mission(...); def task(...); def pose(...); def status(...); def telemetry(...)
    def candidate(c: Candidate); def visit(v: Visit); def analysis(a: AnalysisResult, cand: Candidate, visit: Visit); def event(...)

# gx10/brain/app.py  FastAPI :8000  (00 §6.11)
```
TypeScript: `spacetimedb/src/schema.ts`, `index.ts` (02 §5–6); `dashboard/src/{main.tsx, App.tsx, stdb/connection.ts, stdb/hooks.ts, api/brain.ts, panels/*}` (06 §4–5).

## 5. The campaign loop (what `RunFSM` does)
1. `/api/mission/*` → (voice: `transcribe`) → `parse_mission` → `upsert_mission(status=parsed)` → `status=active`, FSM → SURVEY.
2. **SURVEY:** for each point in `station_plan`: `turn`/`drive` there → if overhead fresh, `set_pose` → `station` → for each proposal: `/score` → `final_score` → `merge_or_new` → `excluded`? → `upsert_candidate` → `TopK.offer`. Ranks → `upsert_candidate` (rank changes only).
3. **PLAN:** `nn_route(pose, topk)` → `upsert_task` ×k. `suggest` mode → AWAIT_APPROVAL.
4. **INVESTIGATE** each: `approach` (E_LOST_TARGET → outcome `unreachable`) → `scoop` → `/analyze(interest_score=score)` → if `scoop_has_item` false: one retry (re-approach + scoop), then outcome `empty_scoop` → `dump` → `mark_visited` (radius 0.20) → `add_analysis` → re-run `excluded` over remaining candidates → recompute the NN route from the new pose.
5. **RETURN:** drive home → `end_run` → `POST mapper /fuse/{run_id}` → run status `done`.
- Every transition: `log_event` + events.jsonl. Every 0.5 s: `upsert_rover_status`.

## 6. Stop and E-STOP
Commands from the dashboard run in order through the FSM. `stop` and E-STOP skip that queue: `/api/estop` calls `RoverLink.estop()` directly, and `stop` preempts the current op. Neither waits on the FSM lock or on any STDB write.

## 7. Build order, checkpoints, acceptance
| Done by | Build | ✅ Checkpoint |
|---|---|---|
| by I0 | Repo skeleton (tree from 00 §2), `shared/py` schemas + tests, router + IPs, STDB install + module v0 (run, pose, rover_status, event) + `claim_writer`, `fake_brain.py` | **I0:** STDB HTTP positional-arg call works (`insert_pose` via `StdbClient`) **[resolves U-11]**; dashboard skeleton shows a pose from fake_brain; brain connects to fake_rover |
| by I1 | All 15 tables + reducers + bindings; brain app, RoverLink, FSM skeleton (SURVEY only), BrainDb; dashboard shell + ConnectionBadge + EStop + QuickBar + MissionPanel (text) | **I1:** text mission → fake_rover station → candidates in STDB → TaskQueuePanel |
| by I2 | scoring, dedupe, TopK, nn_route, visited; INVESTIGATE with fake_rover/fake_vision; overhead_link | **I2:** full campaign on mocks; E-STOP immediate (06 D4) |
| by I3 | Real rover integration with P2; remaining panels; TimelineScrubber + PinDetailDrawer; replay_run | **I3:** real rover runs a mission end to end; replay works |
| by I4 | Voice: Grok STT + Grok parsing with your xAI key, then the offline path (`faster-whisper` on the GX10, U-15, + regex) with the network cut; asr + nlu with 10 test utterances (8/10 parse to the expected Mission), wiring; suggest mode; failure paths (empty scoop, unreachable, link loss) | **I4:** voice mission; 3 sampled pins with Bradford panel in replay |
| by I5 | Soak tests (3 consecutive runs), `reset_db.sh`, docs updated to as-built | **I5 freeze** |

**Acceptance:** 02 §12 (STDB 1–6), 06 D1–D6, plus brain unit tests: `test_scoring.py` (golden numbers from 00 §8), `test_topk.py` (heap keeps the k best under random insert order), `test_dedupe.py`, `test_visited.py`, `test_route.py`, `test_stop.py` (stop/estop bypass the FSM queue), `test_fsm.py` (scripted fake_rover run reaches DONE; estop from every state → ESTOP).

## 8. Mocks
- **You provide** `fake_brain.py` (writes a scripted 3-minute run to STDB through the real reducers, incl. 2 analysis rows with the golden assay: everyone can build the dashboard without hardware) and `replay_run.py` (events.jsonl → fresh DB).
- **You use** `fake_rover.py` (P2), `fake_vision.py` and `fake_mapper.py` (P3).

## 9. Watch-outs
- **Never block the control loop on STDB** (queue + rate limit; events.jsonl backup).
- E-STOP path: dashboard → `/api/estop` → `RoverLink.estop()` directly, no FSM lock. Test it every milestone.
- Grok needs internet: on timeout (`asr_timeout_s` / `nlu_timeout_s`) or any API error, fall back to `faster-whisper` / the regex parser, list what it assumed, and log which path ran (`event` kind `voice_offline`). Always validate Grok's output with the `Mission` pydantic model too, and clamp to the 00 §6.1 ranges.
- `XAI_API_KEY` lives in `config/.env` only. Never log it or send it to the dashboard.
- Heartbeats must keep flowing while the FSM awaits long ops, or the Pi stops mid-drive.
- After every schema change: republish (`--delete-data=always` in dev), rerun `claim_writer`/`add_writer`, regenerate bindings.
- Rank updates can flood `upsert_candidate`; only write rows whose rank/status changed.
- Keep `config/run.yaml` and 00 §8 in sync (a test compares them).
- You approve contract changes; bump `CONTRACT_VERSION` and update every role file's contract copy.

## 10. Contracts you need (copies; source of truth = [00](../00-overview-and-contracts.md))
**Ports:** brain :8000 (yours), STDB :3000 (yours), vision :8100, mapper :8200, overhead WS :8300/pose, Pi WS :8765/rover, Grok `https://api.x.ai/v1`, dashboard :5173.
**Rover ops:** `start_run, drive{dist_m,speed}, turn{deg,speed}, stop, estop, clear_estop, set_pose{x,y,heading_deg,source}, station{station_id,n_views:8,step_deg:45}, approach{cand_id,x,y}, scoop{sample_id}, dump, scoop_pose{pose}, snapshot, heartbeat`; replies `ack` (≤ 0.5 s) / `done{result}` / `err{code}`; codes `E_ESTOP E_BUSY E_TIMEOUT E_BAD_ARGS E_TILT E_CAMERA E_LOST_TARGET E_UPLOAD E_SCOOP E_INTERNAL`. Timeouts: station 60 s, approach 45 s, scoop 30 s. Heartbeat 0.25 s.
**REST (yours):** `POST /api/run/start, /api/run/end, /api/mission/voice (audio/webm), /api/mission/text {text}, /api/approve {mission_id, approve}, /api/quick {cmd}, /api/estop, /api/estop/clear`, `GET /api/health`; every command returns `{accepted, cmd_uid}`. `stop` and estop bypass the FSM queue.
**Scoring:** `score = 0.35·novelty + 0.25·visual + 0.15·pickable + 0.15·category_score − 0.10·min(dist/radius,1)`; dedupe ≤ 0.15 m & cos ≥ 0.85, or ≤ 0.06 m; exclusion radius 0.20 m; min-heap capped at k; NN route recomputed after each visit; reason ≤ 140 chars.
**Vision:** `/score {run_id, items[{prop_id,crop_url,size_m,xy}]}` → components; `/analyze {run_id, sample_id, cand_id, image_url, interest_score}` → `AnalysisResult` + `assay` (00 §6.7). Mapper: `POST /fuse/{run_id}`.
**Reducers you call:** `start_run, end_run, upsert_mission, upsert_task, insert_pose, insert_telemetry, upsert_rover_status, upsert_candidate, mark_visited, add_analysis, log_event` (arg order in 02 §6). `add_analysis` maps `assay` fields → `assayType, a595, concentration, units, verdict, inLinearRange, standardCurveParams=json.dumps(...), simulated, seed, modelVersion`; empty scoop → `""`, `-1`, `-1`, `""`, `""`, `false`, `"{}"`, `true`, `0`, `""`.
