# 04: Integration plan

**Owner: P4** (integration lead). Milestones are **hard gates**: at each one, everyone stops for ≤ 15 min, runs the check together, and writes the result to `docs/requests/milestones.md`. If a gate fails, the owner says what they'll cut (see the cut list in [05](05-unknowns-and-risks.md#risk-register)).

## 1. Principle: mocks first, swap later
Every service has a mock that speaks its exact contract by I0, so nobody waits on anybody:

| Mock | Owner | Stands in for | Ready by gate |
|---|---|---|---|
| `mocks/fake_rover.py` | P2 | Pi WS :8765 (+ station uploads with canned bundles) | I0 |
| `mocks/fake_vision.py` | P3 | vision :8100 (`/analyze` uses the real `bradford_sim`) | I0 |
| `mocks/fake_mapper.py` | P3 | mapper :8200 | I0 |
| `mocks/fake_brain.py` | P4 | the whole backend, writing a scripted run to STDB | I0 |
| `mocks/replay_run.py` | P4 | re-sends a run's `stdb_calls.jsonl` journal into a DB (fresh, or `--as` a new run id) | I3 |

Swap order: fake_mapper → real mapper (I1), fake_vision → real vision (I2), fake_rover → real Pi (I3).

## 2. Milestones
| Gate | Check (all must pass) | Owners |
|---|---|---|
| **I0: Wiring + plumbing** | (a) Router up; all hosts on static IPs; ping each. (b) STDB v2.13.0 running on the GX10; module v0 published; **HTTP reducer call with a positional JSON array works** (`StdbClient.call("insert_pose", …)`) → resolves U-11. (c) Brain ↔ fake_rover WS heartbeat; brain ↔ Pi WS stub heartbeat. (d) Dashboard skeleton shows a pose from fake_brain. (e) Hardware unknowns U-1…U-8 filled; NEED parts ordered/borrowed. (f) AI4Mars subset download started | all |
| **I1: First data flow** | Text mission → brain → fake_rover `station` → **real mapper** stores the bundle → `local_map_tile` + `keyframe` in STDB → dashboard map shows the tile at the right pose. Real motors driven by brain `drive`/`turn`. **Bradford golden test green** (P3) | P2, P3, P4 |
| **I2: Campaign on mocks** | Full loop survey → score (**real vision**) → top-k → approach/scoop (fake_rover) → `/analyze` (assay) → dump → `mark_visited` → return; E-STOP immediate; visited rings + exclusion work; ranks and reasons in TaskQueuePanel. Real rover: station capture works standalone | P3, P4 (P2 standalone) |
| **I3: Real rover end to end** | Real Pi runs a 1-station, k=2 mission: real approach + scoop on pebbles; fused global map appears in replay; scrubber works | all |
| **I4: Feature complete** | Voice mission works; replay pin drawer with photo, score, reason, analysis and **BradfordPanel** (06 D6–D9); multi-station survey; empty-scoop retry path tested | all |
| **I5: Freeze** | 3 consecutive full runs with no code change in between; `reset_db.sh`; docs updated to as-built. After this: **bug fixes only**, no features | all |
| After I5 | Repairs, battery rotation, sleep in shifts (two people awake at all times), rehearse the run setup (not a demo script) | all |

## 3. Per-person plan
| Phase | P1 (hardware) | P2 (rover runtime) | P3 (perception/AI) | P4 (brain/data lead) |
|---|---|---|---|---|
| Start → I0 | Inventory, unknowns U-1…U-8, order parts | Pi OS, Wi-Fi, `fake_rover` | Downloads, venv, `fake_vision`, `fake_mapper` | Repo skeleton, router, STDB v0, `shared/py`, I0 run |
| I0 → I1 | Chassis, power, TB6612 harness, I2C deck | motors, servos, imu, camera, safety, WS server | `bradford_sim` + tests, mapper ingest + tiles; ARM64 lib checks | All tables/reducers, brain skeleton, dashboard shell, `fake_brain` |
| I1 → I2 | Mast, scoop arm, overhead stand, markers | Calibration, pose/heading, station, uploader | Priors, embeddings, heads, `/score`, `/analyze`; MapView v1; BEV + rock proposals on the Pi (`bev.py`, `proposals.py`, from P2) | Scoring, TopK, route, dedupe, exclusion, panels |
| I2 → I3 | Arena, pebbles, scoop tuning with P2; arena photo session + labels | Approach, scoop sequence, overhead tracker | Few-shot training on P1's arena photos; `/fuse` (DR) | Real rover integration, scrubber, drawer, `replay_run` |
| I3 → I4 | Hardening, strain relief, spares | VO fusion, reconnect/robustness | Posegraph, BradfordPanel/Cuvette/Chart | Voice ASR/NLU + wiring, suggest mode, failure paths |
| I4 → I5 | Pre-flight checklist, battery rotation | Threshold tuning, bug fixes | Captions (optional), polish | Soak runs, docs to as-built, freeze |
| After I5 | Repairs | Bug fixes | Bug fixes | Bug fixes, run coordination |

## 4. Daily rhythm
- Stand-up at every gate (5 min: done / next / blocked).
- `main` must always run `make test`. A branch merges only after the whole suite passes, whoever wrote it.
- Anyone may change a contract without approval: bump `CONTRACT_VERSION`, update the role-file copies and announce it ([AGENTS.md §3](../AGENTS.md#3-changing-a-contract)).
