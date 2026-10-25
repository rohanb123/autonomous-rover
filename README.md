## Inspiration

NASA's Curiosity and Perseverance rovers run a system called **AEGIS** that lets them pick science targets by themselves. Without waiting about 20 minutes for Earth to reply, the rover looks at an image, picks the most interesting rock, and points its laser at it. We found that amazing, but also limited: **one image, one target, one instrument**, and the parameters are uploaded in advance by engineers.

So we asked: what if you could just *talk* to a rover? You'd tell it **"Survey within a meter and a half, find the three most interesting rocks, sample them, and come home,"** and it would run a whole science campaign on its own. It would explain every decision, and the whole team could watch it live from "Earth".

That's **Grover**.

## What it does

Grover is a **voice-tasked autonomous science rover**. You give it a mission in plain English and it does the rest:

1. **Understands the mission.** Push-to-talk on the dashboard sends your voice to **Grok's speech-to-text**. Grok then parses it into a validated mission: search radius, number of rocks, what to look for. You can also type it. If the internet drops, a local **faster-whisper** model and a regex parser take over.
2. **Surveys.** At each survey station, Grover spins in place in 8 steps of 45° and takes a photo at each one. It stitches the photos into a **bird's-eye-view ground map** and finds rocks with a classical vision pipeline inspired by Mars rover target finding.
3. **Scores every rock** with a model trained on **real Mars data**:
   `0.35·novelty + 0.25·visual interest + 0.15·pickability + 0.15·category prior − 0.10·distance`
   The category priors come from what real scientists and AEGIS chose in the public **ChemCam target list**.
4. **Plans and executes.** It keeps the best *k* rocks in a min-heap, removes duplicates, plans a nearest-neighbour route, drives to each rock, and takes a close-up "sample" photo.
5. **Remembers where it has been.** Every visited spot gets an exclusion radius, so Grover never re-samples the same place.

Everything streams live into **SpacetimeDB**. One dashboard shows the run as it happens: rover position and trail, the task queue, the top-*k* rocks with scores and reasons, visited rings, live camera frames, health, and an **E-STOP**. Afterwards, the same dashboard **replays the entire run** on an interactive **3D map** of the arena.

## How we built it

**Hardware**
- 3D-printed chassis
- Raspberry Pi 5
- Camera Module 3
- MPU-6050 IMU (heading)
- HC-SR04 ultrasonic rangefinder
- L293D motor driver
- An **ASUS Ascent GX10** (NVIDIA GB10) as the "mission control" computer

**The architecture has two machines:**
- **Rover (Pi):** motion, safety watchdog, station survey, ground-image warping, rock finding, a store-and-forward upload queue on the SD card, and a live MJPEG camera stream
- **GX10:** the **brain** (mission state machine, planner, heap, rover link, voice), a React + Vite dashboard with **audio input via Bluetooth** headphones, **vision** (embeddings and scoring), the **mapper** (ingest and global map fusion), and **SpacetimeDB**

### SpacetimeDB is the system of record

SpacetimeDB isn't a logging add-on. **It is the live shared state of the whole mission.** We wrote a TypeScript module with **13 public tables** and **17 reducers**. Every process writes to it, and the dashboard renders straight from subscriptions:

| Feature | SpacetimeDB tables / reducers |
|---|---|
| **Live location and trail** | `pose` via `insert_pose` (rate-limited at 2 Hz), and `rover_status` via `upsert_rover_status` (link health, state machine state, E-STOP) |
| **Rock queue and ranking** | `candidate` via `upsert_candidate` (score components, rank, reason), and `task_queue` via `upsert_task` (the live plan, reordered as the heap changes) |
| **Mission from voice** | `mission` via `upsert_mission` (parsed parameters plus any assumptions Grok made) |
| **Never revisit a spot** | `visit` via `mark_visited` (location, exclusion radius, outcome). The planner and the 3D map both read it. |
| **Maps** | `keyframe`, `local_map_tile` and `global_tile` via the `upsert_*` reducers, written by the mapper as each station arrives and after global fusion |
| **Science results** | `analysis_record` via `add_analysis`, which holds the simulated Bradford assay. The reducer **rejects any record not marked `simulated`**, so fake data can never pass as real. |
| **Telemetry and explainability** | `telemetry` (IMU, sonar range, upload backlog), and `event` via `log_event` (every decision and error, in plain language) |
| **Runs and replay** | `run` via `start_run`, `end_run` and `set_run_fused`. Our replay tool re-sends a recorded run's writes into a fresh database, so the dashboard's replay mode uses exactly the same code as live mode. |
| **Security** | A private `writer` table with `claim_writer` and `add_writer`. Only the brain and mapper identities can write. The dashboard connects anonymously and is **read-only by design**. |

The Python services write over SpacetimeDB's HTTP reducer API through a small client with rate-limited, coalescing writers, so a burst of telemetry never floods the database. The dashboard never polls the rover: **if it's in SpacetimeDB, it's on screen within milliseconds.**

### Grok and the xAI ecosystem

- **Grok Bot** was our planning partner from hour zero. We used it to write the full spec: contracts, the data schema, the network map, the role briefs for all four teammates, and the risk register. It became the repo's "single ground truth".
- **Grok voice:** xAI's **speech-to-text API** transcribes the push-to-talk mission. We bias it towards our rock vocabulary.
- **Grok parsing:** Grok turns the transcript into a mission object using a **strict JSON schema**. Every value it defaulted or clamped is listed in an `assumptions` field and shown to the operator.

### Claude and Cursor

- **Claude Code** and **Cursor** worked as our pair-programmers on every subsystem. It built the WebGL 3D map, the bird's-eye-view warping and rock finder, the vision service, and the brain's state machine fixes, each with tests. We gave it a rulebook (`AGENTS.md`): area owners, contracts that can't silently change, and "ask before guessing hardware specs". It flagged unconfirmed camera details instead of inventing them, and it refused to let unbuilt rover features return fake "success" codes.

### Perception and ML on real space data

- **Embeddings:** SigLIP ViT-B/16, with logistic-regression heads trained on the **MSL Curiosity labeled image set** and **AI4Mars** terrain labels. The visual-interest head reaches **0.99 test AUC**.
- **Category priors:** built from the **ChemCam master observation list**, including targets AEGIS chose.
- **Rock finding on the Pi:** "Rockster-lite", which uses gradient-adaptive edge detection, contour filtering on solidity, elongation and contrast, and a ground-plane homography. It measures a 10 cm square at 10 ± 1 cm.
- **3D map:** **WebGL2** with no 3D library. It has orbit controls, ground tiles placed exactly in the mission frame, rock pins, visited rings and the rover trail.

## Challenges we ran into

- **Attempting to incorporate a 3-device network.** While we originally planned to reserve the Pi for control, the GX10 for compute, and a laptop for display, we ended up combining the latter two due to technical complexities and to minimize confusion.
- **ARM64 everything.** Installing PyTorch, OpenCV and GPIO libraries on ARM64 Linux (GX10 and Pi) meant broken wheel builds, missing system libraries (`lgpio`), and half-finished installs.
- **Changing the hardware mid-hackathon.** Our 2-servo scoop wasn't going to be reliable in time. We cut it and moved the whole system to version 2.0 of our data contract: a camera close-up `sample` op instead of the scoop, and a sonar `range_m` field. That change touched the rover, brain, database and dashboard at once.
- **Getting frames right.** Mission frame, rover frame, image pixels and ground pixels all have to agree. One stale heading after a survey spin made every later leg go the wrong way until we found and fixed it.

## Accomplishments that we're proud of

- A **full voice-to-rover loop**: speak a mission, watch Grover survey, rank rocks, drive and sample, live on the dashboard.
- Using **SpacetimeDB as the real backbone**. Live mode and replay use the same data and the same code.
- **Real Mars science data** shaping the decisions, not hand-picked weights.
- A **from-scratch WebGL 3D map** that turns a run into something you can interact with and review.
- **Safety first.** There's a 1-second link watchdog, and the E-STOP goes straight to the rover.

## What we learned

- **Contracts first.** Writing the schemas and the network map *before* the code, with Grok Bot's help, let four people build in parallel and still fit together.
- A **real-time database changes how you build a UI**: subscribe, don't poll. The dashboard became much simpler.
- **AI coding tools are most useful when you give them rules.** Claude did its best work when it knew who owned what, and that it must ask instead of guessing.
- **Hardware is the long pole.** Networking, drivers and calibration took longer than any algorithm.

## What's next for Grover

- **On board Life Detection payload and analysis**, we'd love to incorporate a sample collector (scooping arm or drill) that is able to filter the sample through a Bradford Assay and spectrophotometer.
- **Multi-rover campaigns** sharing one SpacetimeDB, so rovers split the survey area and never sample each other's spots.
- **Fully offline field mode.** Captions run on the GX10, voice is already offline-capable, and nothing needs the internet.
