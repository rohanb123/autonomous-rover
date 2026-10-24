# AGENTS.md: rules for every human and coding agent in this repo

## 1. Ground truth
- **Contracts (network, ports, schemas, frames, IDs, timing): [docs/00-overview-and-contracts.md](docs/00-overview-and-contracts.md).** This file wins over everything else, including code, role docs and chat.
- Database schema and reducers: [docs/02-spacetimedb.md](docs/02-spacetimedb.md). The code in `spacetimedb/src/` must match it.
- Dashboard behavior: [docs/06-dashboard.md](docs/06-dashboard.md).
- Hardware, pinout and power: [docs/01-hardware-and-assembly.md](docs/01-hardware-and-assembly.md).
- Your own task list: `docs/roles/P<n>-*.md`. Each role doc ends with a **copy** of the contracts it touches. If that copy disagrees with doc 00, **doc 00 wins**; fix the copy.

## 2. Ownership (MECE)
Every path has exactly one owner (see the tree in doc 00, section 2). **Do not edit files you don't own.** Open a PR or message the owner instead. Agents: if a task needs a change in someone else's path, stop and write the request into `docs/requests/<date>-<from>-to-<owner>.md`.

## 3. Changing a contract
1. Edit `docs/00-overview-and-contracts.md` first (and `docs/02` if it touches the DB).
2. Bump `CONTRACT_VERSION` in doc 00, section 0, and in `shared/py/marsrover_shared/version.py`.
3. Update `shared/py/marsrover_shared/schemas.py` (P4) and the copies at the bottom of the affected role docs.
4. If the DB changed: republish the module, regenerate bindings (`scripts/gen_bindings.sh`), and commit the bindings.
5. Announce in team chat: `CONTRACT vX→vY: <one line>`.

Never change a port, a field name, a unit or a frame silently.

## 4. Conventions
- **Units**: meters, degrees, seconds (unix epoch, float). Never cm/ms in payloads. Pixels only in image-space fields that are suffixed `_px`.
- **Frame**: mission frame; x forward at start, y left, heading CCW in degrees on (-180, 180]. Details in doc 00, section 4.
- **IDs**: `r_YYYYMMDD_HHMMSS` (run), `m_0001` (mission), `s_0001` (station), `k_0001` (keyframe), `p_0001` (proposal), `c_0001` (candidate), `t_0001` (task), `v_0001` (visit), `a_0001` (analysis). All are unique within a run; zero-padded to 4 digits.
- **Python** 3.11+, `ruff` + `black`, type hints, `pydantic` v2 models from `marsrover_shared.schemas` for every message. Do not hand-roll dicts.
- **TypeScript**: strict mode, `npm run lint`. Dashboard uses generated `module_bindings` and never hand-edits them.
- **Logging**: every service appends JSON lines to `data/runs/<run_id>/events.jsonl` and calls `log_event` for anything a judge might want to see.
- **Secrets**: none in git. The SpacetimeDB writer token lives in `config/.env` (gitignored).
- **Honesty markers**: anything we have not verified on our own hardware is tagged `⚠️ UNVERIFIED` in docs and `# UNVERIFIED:` in code. The analysis output is always `simulated: true`.

## 5. Before you push
- `make test` (or the per-dir test command in your role doc) passes.
- If you touched a message, the matching mock in `mocks/` still works.
- No large files: datasets and models go in `ml/data/` and `ml/models/` (gitignored); run data in `data/` (gitignored).

## 6. For coding agents specifically
- Read doc 00 **and** your role doc before writing code. Quote the schema you're implementing in the PR description.
- Prefer the mocks over waiting for teammates.
- Do not invent SpacetimeDB APIs. Use only what is in [docs/02](docs/02-spacetimedb.md) or the official docs linked there. If unsure, write a minimal test against a local `spacetime start`.
