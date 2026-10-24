# 04: Integration timeline (~36 h)

**Owner: P4** (integration lead). H0 = hacking starts. Milestones are **hard gates**: at each one, everyone stops for ≤ 15 min, runs the check together, and writes the result to `docs/requests/milestones.md`. If a gate fails, the owner says what they'll cut (see the cut list in [05](05-unknowns-and-risks.md#risk-register)).

## 1. Principle: mocks first, swap later
Every service has a mock that speaks its exact contract from H2, so nobody waits on anybody:

| Mock | Owner | Stands in for | Ready by |
|---|---|---|---|
| `mocks/fake_rover.py` | P2 | Pi WS :8765 (+ station uploads with canned bundles) | H2 |
| `mocks/fake_vision.py` | P3 | vision :8100 (`/analyze` uses the real `bradford_sim`) | H2 |
| `mocks/fake_mapper.py` | P3 | mapper :8200 | H2 |
| `mocks/fake_brain.py` | P4 | the whole backend, writing a scripted run to STDB | H4 |
| `mocks/replay_run.py` | P4 | re-inserts events.jsonl into a fresh DB | H16 |

Swap order: fake_mapper → real mapper (I1), fake_vision → real vision (I2), fake_rover → real Pi (I3).

## 2. Milestones
| Gate | Time | Check (all must pass) | Owners |
|---|---|---|---|
| **I0: Wiring + plumbing** | **H2** | (a) Router up; all hosts on static IPs; ping each. (b) STDB v2.13.0 running on the GX10; module v0 published; **HTTP reducer call with a positional JSON array works** (`StdbClient.call("insert_pose", …)`) → resolves U-11. (c) Brain ↔ fake_rover WS heartbeat; brain ↔ Pi WS stub heartbeat. (d) Dashboard skeleton shows a pose from fake_brain. (e) Hardware unknowns U-1…U-8 filled; NEED parts ordered/borrowed. (f) AI4Mars subset download started | all |
| **I1: First data flow** | **H8** | Text mission → brain → fake_rover `station` → **real mapper** stores the bundle → `local_map_tile` + `keyframe` in STDB → dashboard map shows the tile at the right pose. Real motors driven by brain `drive`/`turn`. **Bradford golden test green** (P3) | P2, P3, P4 |
| **I2: Campaign on mocks** | **H14** | Full loop survey → score (**real vision**) → top-k → approach/scoop (fake_rover) → `/analyze` (assay) → dump → `mark_visited` → return, with Mars delay on; E-STOP immediate; visited rings + exclusion work; ranks and reasons in TaskQueuePanel. Real rover: station capture works standalone | P3, P4 (P2 standalone) |
| **I3: Real rover end to end** | **H20** | Real Pi runs a 1-station, k=2 mission: real approach + scoop on pebbles; fused global map appears in replay; scrubber works | all |
| **I4: Feature complete** | **H24** | Voice mission works; replay pin drawer with photo, score, reason, analysis and **BradfordPanel** (06 D6–D9); multi-station survey; empty-scoop retry path tested | all |
| **I5: Freeze** | **H28** | 3 consecutive full runs with no code change in between; `reset_db.sh`; docs updated to as-built. After this: **bug fixes only**, no features | all |
| Buffer | H28–H36 | Repairs, battery rotation, sleep in shifts (two people awake at all times), rehearse the run setup (not a demo script) | all |

## 3. Per-person plan
| Hours | P1 (hardware) | P2 (rover runtime) | P3 (perception/AI) | P4 (brain/data lead) |
|---|---|---|---|---|
| H0–H2 | Inventory, unknowns U-1…U-8, order parts | Pi OS, Wi-Fi, `fake_rover` | Downloads, venv, `fake_vision`, `fake_mapper` | Repo skeleton, router, STDB v0, `shared/py`, I0 run |
| H2–H8 | Chassis, power, TB6612 harness, I2C deck | motors, servos, imu, camera, safety, WS server | `bradford_sim` + tests, mapper ingest + tiles; ARM64 lib checks | All tables/reducers, brain skeleton, dashboard shell, `fake_brain` |
| H8–H14 | Mast, scoop arm, overhead stand, markers | Calibration, pose/heading, station, BEV, proposals, uploader | Priors, embeddings, heads, `/score`, `/analyze`; MapView v1 | Scoring, TopK, route, dedupe, exclusion, Delay + Horizons, panels |
| H14–H20 | Arena, pebbles, scoop tuning with P2 | Approach, scoop sequence, overhead tracker | Voice ASR/NLU; arena few-shot session; `/fuse` (DR) | Real rover integration, scrubber, drawer, `replay_run` |
| H20–H24 | Hardening, strain relief, spares | VO fusion, reconnect/robustness | Posegraph, BradfordPanel/Cuvette/Chart | Voice wiring, suggest mode, failure paths |
| H24–H28 | Pre-flight checklist, battery rotation | Threshold tuning, bug fixes | Captions (optional), polish | Soak runs, docs to as-built, freeze |
| H28–H36 | Repairs | Bug fixes | Bug fixes | Bug fixes, run coordination |

## 4. Daily rhythm
- Stand-up every 4 h at the milestone times (5 min: done / next / blocked).
- `main` must always run `make test`. Feature branches merge only after the owner's tests pass.
- A contract change needs a file in `docs/requests/`, P4's OK, a `CONTRACT_VERSION` bump, and updated copies in the role files ([AGENTS.md §3](../AGENTS.md#3-changing-a-contract)).
