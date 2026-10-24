# Team: who is P1–P4

**Owner: P4.** Ownership by path is in [doc 00 §2](00-overview-and-contracts.md#2-repo-tree-and-owners-mece). If this file and doc 00 disagree, doc 00 wins.

| Role | Person | Type | Brief | Owns (summary) |
|---|---|---|---|---|
| **P1** | Aryan | Hardware | [P1-hardware.md](roles/P1-hardware.md) | `hardware/**`, docs/01; chassis, power, wiring, scoop, mast, markers, arena; arena photos + labels (data in `ml/data/arena/`) |
| **P2** | Shrujan | Hybrid (Pi software + bring-up) | [P2-rover-runtime.md](roles/P2-rover-runtime.md) | `rover/**`, `gx10/overhead/**`, `mocks/fake_rover.py` |
| **P3** | Aditi | Hybrid (ML, vision, mapper, map UI) | [P3-perception-ai.md](roles/P3-perception-ai.md) | `ml/**`, `gx10/vision/**`, `gx10/mapper/**`, `dashboard/src/map/**`, `dashboard/src/assay/**`, `config/models.yaml`, `mocks/fake_vision.py`, `mocks/fake_mapper.py`, docs/03 |
| **P4** | Rohan Bharti | Software lead (brain, data, dashboard, integration) | [P4-brain-data.md](roles/P4-brain-data.md) | `gx10/brain/**` (including `voice/`), `spacetimedb/**`, `dashboard/**` except map/assay, `shared/py/**`, `config/**` except `models.yaml`, `scripts/**`, `mocks/fake_brain.py`, `mocks/replay_run.py`, root files, docs 00/02/04/05/06/sources/TEAM |

P4 is the integration lead: they run the milestones in [doc 04](04-integration-timeline.md).

## Rules everyone (and every agent) follows
1. Your role's paths are your default area, but **anyone may work on any path or task** without asking permission. When you touch someone else's area, warn first ("Heads-up: this is Pn's area"), name them in the commit message and tell them in chat ([AGENTS.md §2](../AGENTS.md#2-ownership-default-owners-open-to-everyone)).
2. Contracts (ports, schemas, frames, units, IDs, timing) change only through [AGENTS.md §3](../AGENTS.md#3-changing-a-contract). Anyone may make the change; no approval needed.
3. Build against the mocks, not against teammates' unfinished code.
4. Work on a branch named `p<n>/<topic>` (e.g. `p2/motors`). Merge to `main` only when `make test` passes.

## Kickoff prompt for your coding agent
Paste the block for your role at the start of each new agent session. Claude Code also loads `CLAUDE.md` → `AGENTS.md`, and Cursor reads `AGENTS.md`, automatically.

### P1 (hardware)
```
I am P1 (hardware) on this repo. Read AGENTS.md, docs/00-overview-and-contracts.md §2,
docs/01-hardware-and-assembly.md and docs/roles/P1-hardware.md before doing anything.
My area: hardware/**, docs/01-hardware-and-assembly.md and docs/roles/P1-hardware.md, plus
capturing and labelling arena photos into ml/data/arena/ (gitignored data, layout in docs/03).
You may also work on any other area or task without asking anyone: before you edit
another role's path, tell me "Heads-up: <path> is Pn's area", then go ahead (AGENTS.md §2). If the pinout or anything P2 depends on
changes, tell P2 in chat.
```

### P2 (rover runtime)
```
I am P2 (rover runtime) on this repo. Read AGENTS.md, docs/00-overview-and-contracts.md
(all of it: §6.2 WS protocol, §4 frames/timing, §6.4–6.5 uploads) and docs/roles/P2-rover-runtime.md.
My area: rover/**, gx10/overhead/**, mocks/fake_rover.py and docs/roles/P2-rover-runtime.md.
You may also work on any other area or task without asking anyone: before you edit
another role's path, tell me "Heads-up: <path> is Pn's area", then go ahead (AGENTS.md §2).
Use pydantic models from marsrover_shared.schemas for every message;
never hand-roll dicts and never talk to SpacetimeDB. Hardware pinout comes from docs/01 (P1).
```

### P3 (perception & AI)
```
I am P3 (perception & AI) on this repo. Read AGENTS.md, docs/00-overview-and-contracts.md
(§6.6–6.7 vision API, §6.4 station upload, §8 scoring inputs), docs/02-spacetimedb.md (the reducers
the mapper calls), docs/03-space-data-and-training.md, docs/06-dashboard.md §4–§7 and
docs/roles/P3-perception-ai.md. My area: ml/**, gx10/vision/**, gx10/mapper/**,
dashboard/src/map/**, dashboard/src/assay/**, config/models.yaml,
mocks/fake_vision.py, mocks/fake_mapper.py, docs/03 and docs/roles/P3-perception-ai.md.
You may also work on any other area or task without asking anyone: before you edit
another role's path, tell me "Heads-up: <path> is Pn's area", then go ahead (AGENTS.md §2). Return score *components* only; the final score is the brain's.
Dashboard components take rows via props (MapViewProps, BradfordPanelProps) and never open their own
STDB connection.
```

### P4 (brain, data, dashboard lead)
```
I am P4 (brain/data/dashboard lead and integration lead) on this repo. Read AGENTS.md,
docs/00-overview-and-contracts.md, docs/02-spacetimedb.md, docs/06-dashboard.md and
docs/roles/P4-brain-data.md. My area: gx10/brain/** (including voice/), spacetimedb/**,
dashboard/** except src/map/ and src/assay/, shared/py/**, config/** except models.yaml,
scripts/**, mocks/fake_brain.py, mocks/replay_run.py, root files and docs 00/02/04/05/06/sources/TEAM.
You may also work on any other area or task without asking anyone: before you edit
another role's path, tell me "Heads-up: <path> is Pn's area", then go ahead (AGENTS.md §2).
Any contract change follows AGENTS.md §3 (bump CONTRACT_VERSION, update schemas.py and the
role-doc copies). Never call STDB on the control path; writes go through RateLimitedWriter.
Do not invent SpacetimeDB APIs beyond docs/02.
```
